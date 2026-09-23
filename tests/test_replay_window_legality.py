"""回放窗口合法性（读取侧自愈）测试。

对应《会话工具轨迹持久化》方案 §5.5 / §8：协议消息落盘后，任何时刻回放给
provider 的窗口都不允许含孤儿协议消息——

- 头部：``find_legal_message_start``（既有，复用）修"结果先于声明"；
- 尾部：``find_legal_message_end``（本次新增）修"声明在窗口内、结果被裁掉"；
- 两个接入点：BUILD 安全阀（``AgentLoop._repair_replay_window_head``，
  含 memory skill 裁剪后的历史）与 runner 预算裁剪（``_snip_history``）。

纯文本历史（未开启轨迹落盘）两侧均须为 no-op：这是"开关只控写入"的保证。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop, TurnContext, TurnState
from nanobee.agent.messages import InboundMessage
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.session.session import Session
from nanobee.utils.helpers import find_legal_message_end, find_legal_message_start


def _call(call_ids: list[str], *, content: str | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "cron_create", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _tool(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": "cron_create", "content": content}


def _user(content: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": content}


def _text_assistant(content: str = "done") -> dict[str, Any]:
    return {"role": "assistant", "content": content}


class TestFindLegalMessageEnd:
    """尾部修复纯函数：只裁"尾部未完成的调用组"。"""

    def test_text_only_sequence_is_noop(self) -> None:
        messages = [_user(), _text_assistant(), _user("again")]
        assert find_legal_message_end(messages) == len(messages)

    def test_empty_sequence(self) -> None:
        assert find_legal_message_end([]) == 0

    def test_complete_group_kept(self) -> None:
        messages = [_user(), _call(["c1"]), _tool("c1"), _text_assistant()]
        assert find_legal_message_end(messages) == len(messages)

    def test_trailing_dangling_call_cut(self) -> None:
        messages = [_user(), _call(["c1"]), _tool("c1"), _call(["c2"])]
        assert find_legal_message_end(messages) == 3

    def test_cascaded_dangling_calls_cut(self) -> None:
        """结果整批被裁时，同组上游的声明级联裁掉（否则窗口尾部仍悬空）。"""
        messages = [_user(), _call(["c1"]), _call(["c2"])]
        assert find_legal_message_end(messages) == 1

    def test_partially_fulfilled_group_cut(self) -> None:
        messages = [_user(), _call(["c1", "c2"]), _tool("c1")]
        assert find_legal_message_end(messages) == 1

    def test_mid_window_orphan_not_cascaded_to_zero(self) -> None:
        """窗口中途的孤立调用不在此处处理（交 _backfill_missing_tool_results 补结果）。

        保守语义：一旦右侧存在声明完整的调用组，即停止裁剪，避免把窗口裁空。
        """
        messages = [
            _call(["a1", "a2"]),  # a2 无结果（窗口中部）
            _tool("a1"),
            _call(["b1"]),
            _tool("b1"),
        ]
        assert find_legal_message_end(messages) == len(messages)

    def test_user_boundary_stops_scan(self) -> None:
        messages = [_user(), _call(["c1"])]
        assert find_legal_message_end(messages) == 1

    def test_no_legal_prefix_returns_zero(self) -> None:
        """无 user/system 边界且整段建立在未完成调用上 → 返回 0（契约：无合法非空前缀）。

        调用方自行决定是否接受空窗口（runner 的兜底分支就不接受，见 runner 侧用例）。
        """
        messages = [_call(["c1", "c2"]), _tool("c1")]
        assert find_legal_message_end(messages) == 0

    def test_does_not_mutate_input(self) -> None:
        messages = [_user(), _call(["c1"])]
        snapshot = [dict(message) for message in messages]
        find_legal_message_end(messages)
        assert messages == snapshot


class TestFindLegalMessageStartRegression:
    """头部修复既有语义不变（复用而非另起一套）。"""

    def test_orphan_head_result_skipped(self) -> None:
        messages = [_tool("c9"), _user(), _text_assistant()]
        assert find_legal_message_start(messages) == 1

    def test_legal_sequence_noop(self) -> None:
        messages = [_user(), _call(["c1"]), _tool("c1"), _text_assistant()]
        assert find_legal_message_start(messages) == 0

    def test_text_only_noop(self) -> None:
        assert find_legal_message_start([_user(), _text_assistant()]) == 0


class _StubProvider:
    """只提供 prompt token 估算的 provider 替身（够 _snip_history 使用）。"""

    supports_progress_deltas = False

    def estimate_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> tuple[int, str]:
        return 100_000, "stub"


def _build_spec(*, block_limit: int) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=ToolRegistry(),
        model="stub-model",
        max_iterations=3,
        max_tool_result_chars=4096,
        max_tokens=128,
        # context_window_tokens 非空是 _snip_history 的入口前提；
        # 预算取 context_block_limit（显式值优先）
        context_window_tokens=1000,
        context_block_limit=block_limit,
    )


class TestSnipHistoryTailRepair:
    """runner 预算裁剪后的尾部修复（否则下游 backfill 会把裁掉的组请回来）。"""

    def test_dangling_group_dropped_after_snip(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            _user("u1"),
            _call(["c1"]),
            _tool("c1"),
            _user("x" * 4000),
            _call(["c2"]),  # 悬尾：其结果落在预算之外
        ]
        runner = AgentRunner(_StubProvider())  # type: ignore[arg-type]

        kept = runner._snip_history(_build_spec(block_limit=200), messages)

        assert kept[0]["role"] == "system"
        assert not any(message.get("tool_calls") for message in kept)
        assert kept[-1]["role"] == "user"

    def test_text_only_history_snip_unchanged(self) -> None:
        """纯文本历史：尾部修复为 no-op，既有裁剪口径不变。"""
        messages = [
            {"role": "system", "content": "sys"},
            _user("A" * 4000),
            _text_assistant("y" * 4000),
            _user("B" * 40),
        ]
        runner = AgentRunner(_StubProvider())  # type: ignore[arg-type]

        kept = runner._snip_history(_build_spec(block_limit=200), messages)

        assert kept == [{"role": "system", "content": "sys"}, _user("B" * 40)]

    def test_head_orphan_dropped_by_existing_alignment(self) -> None:
        """头部孤儿仍由既有 user 对齐 + 头部修复处理（回归锁）。"""
        messages = [
            {"role": "system", "content": "sys"},
            _tool("c9"),
            _user("A" * 4000),
            _text_assistant("y" * 4000),
        ]
        runner = AgentRunner(_StubProvider())  # type: ignore[arg-type]

        kept = runner._snip_history(_build_spec(block_limit=200), messages)

        assert kept[0]["role"] == "system"
        assert all(message.get("role") != "tool" for message in kept)

    def test_fallback_keeps_window_when_no_user_exists(self) -> None:
        """无任何 user 的历史：兜底分支不得把窗口裁到只剩 system。

        尾部修复会把主分支窗口裁空 → 进兜底；兜底刻意不做尾部修复，否则模型会
        完全失去上下文（协议合法性由下游 backfill 兜）。
        """
        messages = [
            {"role": "system", "content": "sys"},
            _tool("c9"),
            _call(["c1"]),
        ]
        runner = AgentRunner(_StubProvider())  # type: ignore[arg-type]

        kept = runner._snip_history(_build_spec(block_limit=200), messages)

        assert kept[0]["role"] == "system"
        assert len(kept) > 1, "兜底不应退化成只剩 system"
        assert all(message.get("role") != "tool" for message in kept)


def _make_ctx() -> TurnContext:
    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hi")
    ctx = TurnContext(
        msg=msg,
        context_id="u1",
        session_id="test:c1",
        state=TurnState.BUILD,
        turn_id="u1:1",
    )
    ctx.turn_wall_started_at = time.time()
    return ctx


class TestRepairReplayWindowHead:
    """BUILD 侧自愈：截断把声明切掉后，孤儿结果不得进入回放窗口。"""

    def test_orphan_head_messages_dropped(self, tmp_path: Path) -> None:
        loop = object.__new__(AgentLoop)
        session = Session(session_id="test:c1", user_id="u1")
        session.messages = [_tool("c9"), _user(), _text_assistant()]

        loop._repair_replay_window_head(_make_ctx(), session)

        assert session.messages == [_user(), _text_assistant()]

    def test_legal_history_untouched(self) -> None:
        loop = object.__new__(AgentLoop)
        session = Session(session_id="test:c1", user_id="u1")
        original = [_user(), _call(["c1"]), _tool("c1"), _text_assistant()]
        session.messages = original

        loop._repair_replay_window_head(_make_ctx(), session)

        # 合法历史必须原地不动（同一 list 对象，零拷贝零改写）
        assert session.messages is original
