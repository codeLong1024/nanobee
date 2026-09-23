"""会话工具轨迹落盘（SAVE 侧）测试。

对应《会话工具轨迹持久化》方案 §5.2-§5.7 / §8 测试矩阵：增量切片锚点、
失败轮留痕、配对校验与悬尾占位、三层清洗、出生点脱敏、开关回退、
ctx 不被改写、出站零污染。

测试用最小化 AgentLoop（``object.__new__`` 绕过重型构造器）直接驱动
``_state_save``，与 tests/test_fresh_session.py 的既有手法一致。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop, TurnContext, TurnState
from nanobee.agent.messages import InboundMessage
from nanobee.config.schema import AgentDefaults, Config
from nanobee.session.session_manager import SessionManager
from nanobee.utils.helpers import build_runtime_context


class _StubProvider:
    """只提供默认模型名的 provider 替身（够 AgentLoop 构造使用）。"""

    def get_default_model(self) -> str:
        return "stub-model"


def _system(content: str = "sys") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant_call(
    call_ids: list[str],
    *,
    name: str = "cron_create",
    arguments: str = '{"cron": "0 9 * * *"}',
    reasoning: str | None = None,
) -> dict[str, Any]:
    """构造一条 assistant(tool_calls) 消息（可带思维链键）。"""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for call_id in call_ids
        ],
    }
    if reasoning is not None:
        message["reasoning_content"] = reasoning
        message["thinking_blocks"] = [{"type": "thinking", "thinking": reasoning}]
    return message


def _tool_result(
    call_id: str,
    content: str = "ok",
    *,
    name: str = "cron_create",
) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def _assistant_text(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _new_loop(
    tmp_path: Path,
    *,
    persist: bool = True,
    reasoning: bool = False,
    result_chars: int = 8192,
    args_chars: int = 8192,
) -> AgentLoop:
    """构造可驱动 _state_save 的最小 AgentLoop 并补齐落盘相关实例属性。"""
    loop = object.__new__(AgentLoop)
    loop.session_manager = SessionManager(tmp_path / "users")
    loop.event_bus = None
    loop._persist_tool_traces = persist
    loop._persist_reasoning = reasoning
    loop._tool_result_persist_max_chars = result_chars
    loop._tool_args_persist_max_chars = args_chars
    return loop


def _make_ctx(*, final_content: str | None = None, error: str | None = None) -> TurnContext:
    msg = InboundMessage(
        channel="test",
        sender_id="u1",
        chat_id="c1",
        content="建个定时任务",
    )
    ctx = TurnContext(
        msg=msg,
        context_id="u1",
        session_id="test:c1",
        state=TurnState.SAVE,
        turn_id="u1:1",
    )
    ctx.final_content = final_content
    ctx.error = error
    ctx.turn_wall_started_at = time.time()
    return ctx


def _drive(
    loop: AgentLoop,
    ctx: TurnContext,
    *,
    initial: list[dict[str, Any]],
    increment: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按切片契约装配 initial/all_messages，驱动 SAVE 并返回落盘后的会话消息。"""
    ctx.initial_messages = list(initial)
    ctx.all_messages = [*initial, *increment]
    asyncio.run(loop._state_save(ctx))
    session = loop.session_manager.get_or_create(ctx.context_id, ctx.session_id)
    return session.messages


def _roles(messages: list[dict[str, Any]]) -> list[str]:
    return [str(message.get("role")) for message in messages]


_INITIAL = [_system(), _user("建个定时任务")]


class TestIncrementSlice:
    """增量切片：锚点 = len(initial_messages)，本轮用户消息不重复落盘。"""

    def test_protocol_messages_persisted_in_order(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="已创建")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "task_id=997b042a-bf5"),
                _assistant_text("已创建"),
            ],
        )

        assert _roles(messages) == ["assistant", "tool", "assistant"]
        assert messages[0]["tool_calls"][0]["id"] == "call_1"
        assert messages[1]["tool_call_id"] == "call_1"
        assert messages[1]["name"] == "cron_create"
        assert messages[2]["content"] == "已创建"

    def test_already_persisted_user_message_not_duplicated(self, tmp_path: Path) -> None:
        """本轮用户消息在 BUILD 已落盘，SAVE 的增量切片必须跳过它。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1"]), _tool_result("call_1")],
        )

        assert "user" not in _roles(messages)

    def test_multi_iteration_groups_all_persisted(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1"),
                _assistant_call(["call_2"]),
                _tool_result("call_2"),
                _assistant_text("done"),
            ],
        )

        assert _roles(messages) == ["assistant", "tool", "assistant", "tool", "assistant"]

    def test_empty_initial_messages_abandons_slice(self, tmp_path: Path) -> None:
        """锚点前提不满足时放弃切片：宁可缺增量，不可重复/错位落历史。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=[],
            increment=[_assistant_call(["call_1"]), _tool_result("call_1")],
        )

        # 只有终文本（协议消息因切片前提不满足被整体放弃）
        assert messages == [{"role": "assistant", "content": "done"}]

    def test_all_messages_shorter_than_boundary_abandons_slice(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=[_system(), _user("hi"), _user("hi2")],
            increment=[],
        )

        assert messages == [{"role": "assistant", "content": "done"}]

    def test_final_text_persisted_at_its_increment_position(self, tmp_path: Path) -> None:
        """终文本按增量原位落盘（保序），而不是被搬到会话尾部。

        max_iterations 出口会先追加终文本、再追加注入的 user 消息
        （runner `_append_final_message` → `_try_drain_injections`），落盘顺序
        必须与 runner 内部真实顺序一致，否则历史因果顺序被改写。
        """
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="到上限了")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1"),
                _assistant_text("到上限了"),
                _user("追加要求"),
            ],
        )

        assert _roles(messages) == ["assistant", "tool", "assistant", "user"]
        assert messages[2]["content"] == "到上限了"
        assert messages[3]["content"] == "追加要求"

    def test_no_increment_no_write(self, tmp_path: Path) -> None:
        """无增量且无终文本：保持旧语义，不产生落盘动作。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="boom")
        _drive(loop, ctx, initial=_INITIAL, increment=[])

        assert loop.session_manager.store.load("u1", "test:c1") is None


class TestFailureTurnPersistence:
    """失败 / 中断轮必须留痕（有增量即保存）。"""

    def test_failed_turn_writes_protocol_messages(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="tool error")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1"]), _tool_result("call_1", "boom")],
        )

        assert _roles(messages) == ["assistant", "tool"]
        # 有增量即写盘（不需要终文本）
        assert loop.session_manager.store.load("u1", "test:c1") is not None

    def test_failed_turn_has_no_placeholder_final(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="boom")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1"])],
        )

        assert not any(message.get("content") == "" for message in messages)


class TestPairingValidation:
    """配对校验（照抄 nanobot _save_turn 语义）。"""

    def test_orphan_tool_result_dropped(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="boom")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_tool_result("call_x")],
        )

        assert messages == []

    def test_tool_result_without_id_dropped(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="boom")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[{"role": "tool", "content": "no id"}],
        )

        assert messages == []

    def test_duplicate_tool_result_dropped(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "first"),
                _tool_result("call_1", "second"),
                _assistant_text("done"),
            ],
        )

        tool_entries = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0]["content"] == "first"

    def test_result_seeded_from_history_is_dropped(self, tmp_path: Path) -> None:
        """跨 turn：历史中已 fulfilled 的结果再次出现在增量里 → 丢弃。"""
        loop = _new_loop(tmp_path)
        session = loop.session_manager.get_or_create("u1", "test:c1")
        session.add_protocol_message(_assistant_call(["call_0"]))
        session.add_protocol_message(_tool_result("call_0", "old"))
        loop.session_manager.save(session)

        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_tool_result("call_0", "again"), _assistant_text("done")],
        )

        tool_entries = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0]["content"] == "old"

    def test_result_declared_in_history_is_kept(self, tmp_path: Path) -> None:
        """崩溃恢复：声明在上一个 turn，结果在本轮补落 → 保留。"""
        loop = _new_loop(tmp_path)
        session = loop.session_manager.get_or_create("u1", "test:c1")
        session.add_protocol_message(_assistant_call(["call_0"]))
        loop.session_manager.save(session)

        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_tool_result("call_0", "recovered"), _assistant_text("done")],
        )

        tool_entries = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0]["content"] == "recovered"


class TestDanglingRepair:
    """悬尾修复（对 nanobot 的有意增强）：声明无结果 → 合成取消占位。"""

    def test_dangling_call_gets_cancelled_placeholder(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="cancelled")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1"])],
        )

        assert _roles(messages) == ["assistant", "tool"]
        assert messages[1]["tool_call_id"] == "call_1"
        assert messages[1]["name"] == "cron_create"
        assert "cancelled" in messages[1]["content"]

    def test_partial_group_placeholder_only_for_missing_the_missing_call(self, tmp_path: Path) -> None:
        """同一 assistant 声明两个调用、只有一个结果 → 只给缺的那个补占位。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="cancelled")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1", "call_2"]), _tool_result("call_1")],
        )

        placeholders = [
            m for m in messages if m.get("role") == "tool" and "cancelled" in str(m.get("content"))
        ]
        assert len(placeholders) == 1
        assert placeholders[0]["tool_call_id"] == "call_2"
        # 真实结果仍按原样落盘
        assert any(m.get("content") == "ok" for m in messages)

    def test_no_placeholder_when_result_follows_in_increment(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "real"),
                _assistant_text("done"),
            ],
        )

        assert len([m for m in messages if m.get("role") == "tool"]) == 1
        assert "cancelled" not in str(messages[1]["content"])


class TestCleaning:
    """三层清洗：思维链剥离、结果/参数限长、注入尾注剥离。"""

    def test_reasoning_stripped_by_default(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"], reasoning="内部推理"),
                _tool_result("call_1"),
                _assistant_text("done"),
            ],
        )

        assert "reasoning_content" not in messages[0]
        assert "thinking_blocks" not in messages[0]

    def test_reasoning_kept_when_enabled(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, reasoning=True)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"], reasoning="内部推理"),
                _tool_result("call_1"),
                _assistant_text("done"),
            ],
        )

        assert messages[0]["reasoning_content"] == "内部推理"
        assert messages[0]["thinking_blocks"]

    def test_tool_result_truncated_with_marker(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, result_chars=5)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "0123456789"),
                _assistant_text("done"),
            ],
        )

        assert messages[1]["content"] == "01234\n(persist truncated)"

    def test_tool_args_truncated_with_marker(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, args_chars=5)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"], arguments='{"a": 123456789}'),
                _tool_result("call_1"),
                _assistant_text("done"),
            ],
        )

        arguments = messages[0]["tool_calls"][0]["function"]["arguments"]
        assert arguments.startswith('{"a":')
        assert "persist truncated" in arguments

    def test_short_tool_result_not_marked(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, result_chars=100)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "short"),
                _assistant_text("done"),
            ],
        )

        assert messages[1]["content"] == "short"

    def test_injected_user_runtime_context_stripped(self, tmp_path: Path) -> None:
        runtime = build_runtime_context(
            channel="test",
            chat_id="c1",
            sender_id="u1",
            history=[],
            system_prompt="",
            ctx_window=1000,
        )
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_text("先说一句"),
                _user(f"追加要求\n\n{runtime}"),
                _assistant_text("done"),
            ],
        )

        assert _roles(messages) == ["assistant", "user", "assistant"]
        assert messages[1]["content"] == "追加要求"


class TestRedaction:
    """出生点脱敏：落盘内容即已掩码。"""

    def test_secret_in_tool_result_masked(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "api_key=sk-super-secret-value"),
                _assistant_text("done"),
            ],
        )

        assert "sk-super-secret-value" not in messages[1]["content"]
        assert "<redacted>" in messages[1]["content"]

    def test_secret_in_tool_args_masked(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"], arguments='{"api_key": "sk-super-secret-value"}'),
                _tool_result("call_1"),
                _assistant_text("done"),
            ],
        )

        arguments = messages[0]["tool_calls"][0]["function"]["arguments"]
        assert "sk-super-secret-value" not in arguments
        assert "<redacted>" in arguments


class TestSwitchAndIsolation:
    """总开关回退 + ctx 零改写 + 出站零污染。"""

    def test_switch_off_falls_back_to_legacy_shape(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, persist=False)
        ctx = _make_ctx(final_content="done")
        messages = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1"),
                _assistant_text("done"),
            ],
        )

        assert messages == [{"role": "assistant", "content": "done"}]

    def test_switch_off_failed_turn_writes_nothing(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, persist=False)
        ctx = _make_ctx(error="boom")
        _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_call(["call_1"]), _tool_result("call_1")],
        )

        assert loop.session_manager.store.load("u1", "test:c1") is None

    def test_ctx_and_increment_left_untouched(self, tmp_path: Path) -> None:
        """SAVE 只做浅拷贝处理：不改写 ctx.final_content / ctx.all_messages / 原始消息。"""
        loop = _new_loop(tmp_path, reasoning=False, result_chars=4)
        ctx = _make_ctx(final_content="done")
        initial = list(_INITIAL)
        increment = [
            _assistant_call(["call_1"], reasoning="内部推理"),
            _tool_result("call_1", "0123456789"),
            _assistant_text("done"),
        ]
        ctx.initial_messages = initial
        ctx.all_messages = [*initial, *increment]

        before = [dict(message) for message in ctx.all_messages]
        asyncio.run(loop._state_save(ctx))

        assert ctx.final_content == "done"
        assert len(ctx.all_messages) == len(before)
        # 原始对象未被清洗（思维链仍在、结果未被截断）
        assert increment[0]["reasoning_content"] == "内部推理"
        assert increment[1]["content"] == "0123456789"


class TestConfigWiring:
    """四项落盘配置必须经 schema → from_kernel → __init__ 全链贯通（漏接即红）。

    其余用例为提速用 `object.__new__` 造最小实例，会绕过构造器；此处专门走真实
    `from_kernel` 构造，覆盖"schema 加字段但漏透传/漏接构造参数"这类静默漏接。
    """

    def test_defaults_are_off_and_bounded(self) -> None:
        defaults = AgentDefaults()
        assert defaults.persist_tool_traces is False
        assert defaults.persist_reasoning is False
        assert defaults.tool_result_persist_max_chars == 8192
        assert defaults.tool_args_persist_max_chars == 8192

    def test_from_kernel_wires_persistence_settings(self, tmp_path: Path) -> None:
        cfg = Config()
        defaults = cfg.agents.defaults
        defaults.persist_tool_traces = True
        defaults.persist_reasoning = True
        defaults.tool_result_persist_max_chars = 111
        defaults.tool_args_persist_max_chars = 222

        loop = AgentLoop.from_kernel(
            provider=_StubProvider(),  # type: ignore[arg-type]
            workspace=tmp_path,
            context_manager=None,
            context_pipeline=None,
            event_bus=None,
            plugin_manager=None,
            config=cfg,
        )

        assert loop._persist_tool_traces is True
        assert loop._persist_reasoning is True
        assert loop._tool_result_persist_max_chars == 111
        assert loop._tool_args_persist_max_chars == 222

    def test_outbound_content_unaffected_by_trace_persistence(self, tmp_path: Path) -> None:
        """落盘含协议消息后，出站内容仍只由终文本组装（零污染）。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="已创建")
        _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_call(["call_1"]),
                _tool_result("call_1", "task_id=997b042a-bf5"),
                _assistant_text("已创建"),
            ],
        )

        outbound = loop._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            loop._turn_increment(ctx),
            ctx.exit_reason,
            ctx.had_injections,
        )

        assert outbound is not None
        assert outbound.content == "已创建"
        assert "997b042a-bf5" not in outbound.content
