"""会话工具轨迹落盘的**体积预算机制不变量**锁（非经验数值）。

分工说明：经验数值（P50/P90 实测分布、增长率）属评测集 driver 与门槛基线报告，
写进测试会随噪声抖动而变脆；本文件只锁"机制必须成立"的硬不变量：

1. 开关关闭 → 协议行为 0（严格回退旧口径）；
2. 触顶 → 截断标记必现，且落盘长度 == 上界 + 标记长度；
3. 单轮增量字节 ≤ 公式上界 ``n_calls × (参数界 + 结果界 + 2×标记 + 单调用信封) + 文件信封``，
   **与输入体积无关**（喂 1MB 结果也必须落回界内）；
4. 空增量轮零写；失败轮留痕且不落空终文本；悬尾必落取消占位。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop, TurnContext, TurnState
from nanobee.agent.messages import InboundMessage
from nanobee.session.session_audit import (
    PERSIST_TRUNCATED_SUFFIX,
    audit_session_file,
)
from nanobee.session.session_manager import SessionManager

# 单次工具调用的信封余量（id / type / function / name 骨架 + JSON 转义余量）。
# 实测（短 id + 10 字符工具名）约 41 字节/调用，取 256 留足 UUID 长度与转义空间；
# 该常量只作"上界余量"，不参与经验分布表达。
_PER_CALL_ENVELOPE_BYTES = 256

# 会话文件信封（元数据行 + 终文本行）。
_FILE_ENVELOPE_BYTES = 1024


def _turn_bound(
    call_count: int,
    *,
    result_cap: int,
    args_cap: int,
    final_text_len: int = 0,
) -> int:
    """单轮增量字节上界（公式化，取代"单一 KB 数"的口径）。"""
    suffix = len(PERSIST_TRUNCATED_SUFFIX)
    per_call = args_cap + suffix + result_cap + suffix + _PER_CALL_ENVELOPE_BYTES
    return call_count * per_call + _FILE_ENVELOPE_BYTES + final_text_len


def _system(content: str = "sys") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant_calls(call_ids: list[str], arguments: str, *, name: str = "write_file") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id in call_ids
        ],
    }


def _tool_result(call_id: str, content: str, *, name: str = "write_file") -> dict[str, Any]:
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
    loop = object.__new__(AgentLoop)
    loop.session_manager = SessionManager(tmp_path / "users")
    loop.event_bus = None
    loop._persist_tool_traces = persist
    loop._persist_reasoning = reasoning
    loop._tool_result_persist_max_chars = result_chars
    loop._tool_args_persist_max_chars = args_chars
    return loop


def _make_ctx(*, final_content: str | None = None, error: str | None = None) -> TurnContext:
    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="x")
    ctx = TurnContext(
        msg=msg,
        context_id="u1",
        session_id="test:c1",
        state=TurnState.SAVE,
        turn_id="u1:1",
    )
    ctx.final_content = final_content
    ctx.error = error
    return ctx


def _drive(
    loop: AgentLoop,
    ctx: TurnContext,
    *,
    initial: list[dict[str, Any]],
    increment: list[dict[str, Any]],
) -> Path:
    """驱动 SAVE 落盘，返回会话文件路径。"""
    ctx.initial_messages = list(initial)
    ctx.all_messages = [*initial, *increment]
    asyncio.run(loop._state_save(ctx))
    return loop.session_manager.store._session_path("u1", "test:c1")


_INITIAL = [_system(), _user("hi")]


class TestSwitchOffLegacyShape:
    """开关关闭：严格旧口径（协议行 0，只有终文本）。"""

    def test_switch_off_writes_no_protocol_rows(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, persist=False)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 8192),
                _tool_result("call_1", "b" * 8192),
                _assistant_text("done"),
            ],
        )

        report = audit_session_file(path)

        assert report.ok
        assert report.census["protocol_rows"] == 0
        assert report.census["truncated"] == 0
        assert report.message_count == 1

    def test_switch_off_turn_bound_is_minimal(self, tmp_path: Path) -> None:
        """关时体积与输入规模无关（协议消息整体不落盘）。"""
        loop = _new_loop(tmp_path, persist=False)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 1_000_000),
                _tool_result("call_1", "b" * 1_000_000),
                _assistant_text("done"),
            ],
        )

        assert path.stat().st_size < _FILE_ENVELOPE_BYTES


class TestTruncationInvariants:
    """触顶 → 标记必现且长度恰为上界 + 标记。"""

    def test_result_at_cap_truncated_with_marker(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, result_chars=100)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "{}"),
                _tool_result("call_1", "b" * 5000),
                _assistant_text("done"),
            ],
        )

        report = audit_session_file(path)

        assert report.ok
        assert report.census["truncated"] == 1
        assert report.message_count == 3

    def test_args_at_cap_truncated_with_marker(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, args_chars=100)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 5000),
                _tool_result("call_1", "ok"),
                _assistant_text("done"),
            ],
        )

        report = audit_session_file(path)

        assert report.ok
        assert report.census["truncated"] == 1

    def test_truncated_length_equals_cap_plus_marker(self, tmp_path: Path) -> None:
        """截断后长度恰为 ``cap + len(suffix)``（落盘值可预测，便于体积核算）。"""
        loop = _new_loop(tmp_path, result_chars=32)
        ctx = _make_ctx(final_content="done")
        _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "{}"),
                _tool_result("call_1", "b" * 5000),
                _assistant_text("done"),
            ],
        )
        session = loop.session_manager.get_or_create("u1", "test:c1")
        tool_rows = [m for m in session.messages if m.get("role") == "tool"]

        assert tool_rows[0]["content"] == "b" * 32 + PERSIST_TRUNCATED_SUFFIX


class TestVolumeBound:
    """单轮体积上界公式（与输入体积无关）。"""

    def test_single_call_within_bound(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 8192),
                _tool_result("call_1", "b" * 8192),
                _assistant_text("done"),
            ],
        )

        assert path.stat().st_size <= _turn_bound(
            1, result_cap=8192, args_cap=8192, final_text_len=len("done"),
        )

    def test_three_calls_within_bound(self, tmp_path: Path) -> None:
        call_ids = ["call_1", "call_2", "call_3"]
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(call_ids, "a" * 8192),
                *[_tool_result(call_id, "b" * 8192) for call_id in call_ids],
                _assistant_text("done"),
            ],
        )

        assert path.stat().st_size <= _turn_bound(
            len(call_ids), result_cap=8192, args_cap=8192, final_text_len=len("done"),
        )

    def test_huge_input_still_bounded(self, tmp_path: Path) -> None:
        """1MB 结果 + 1MB 参数：落盘仍必须在界内（截断失效即红）。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(final_content="done")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 1_000_000),
                _tool_result("call_1", "b" * 1_000_000),
                _assistant_text("done"),
            ],
        )

        assert path.stat().st_size <= _turn_bound(
            1, result_cap=8192, args_cap=8192, final_text_len=len("done"),
        )

    def test_bytes_grow_with_call_count(self, tmp_path: Path) -> None:
        """单轮成本随调用数线性增长（上界公式的线性项）。"""
        one = _new_loop(tmp_path / "one")
        three = _new_loop(tmp_path / "three")
        call_ids = ["call_1", "call_2", "call_3"]
        one_path = _drive(
            one,
            _make_ctx(final_content="done"),
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "a" * 8192),
                _tool_result("call_1", "b" * 8192),
                _assistant_text("done"),
            ],
        )
        three_path = _drive(
            three,
            _make_ctx(final_content="done"),
            initial=_INITIAL,
            increment=[
                _assistant_calls(call_ids, "a" * 8192),
                *[_tool_result(call_id, "b" * 8192) for call_id in call_ids],
                _assistant_text("done"),
            ],
        )

        assert three_path.stat().st_size > one_path.stat().st_size
        assert three_path.stat().st_size <= 3 * one_path.stat().st_size


class TestReasoningVolume:
    """思维链是体积大头：默认剥离必须显著小于保留。"""

    def test_reasoning_stripped_is_smaller(self, tmp_path: Path) -> None:
        reasoning = "推理" * 2000
        stripped_path = _drive(
            _new_loop(tmp_path / "off"),
            _make_ctx(final_content="done"),
            initial=_INITIAL,
            increment=[
                {
                    **_assistant_calls(["call_1"], "{}"),
                    "reasoning_content": reasoning,
                },
                _tool_result("call_1", "ok"),
                _assistant_text("done"),
            ],
        )
        kept_path = _drive(
            _new_loop(tmp_path / "on", reasoning=True),
            _make_ctx(final_content="done"),
            initial=_INITIAL,
            increment=[
                {
                    **_assistant_calls(["call_1"], "{}"),
                    "reasoning_content": reasoning,
                },
                _tool_result("call_1", "ok"),
                _assistant_text("done"),
            ],
        )

        assert stripped_path.stat().st_size < kept_path.stat().st_size
        assert audit_session_file(stripped_path).census["reasoning_left"] == 0
        assert audit_session_file(kept_path).census["reasoning_left"] == 1


class TestTurnShapeInvariants:
    """轮型不变量：空轮 / 失败轮 / 悬尾轮。"""

    def test_empty_increment_writes_nothing(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="boom")
        _drive(loop, ctx, initial=_INITIAL, increment=[])

        assert not loop.session_manager.store._session_path("u1", "test:c1").exists()

    def test_failed_turn_keeps_trace_without_empty_final(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="tool error")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[
                _assistant_calls(["call_1"], "{}"),
                _tool_result("call_1", "boom"),
            ],
        )

        report = audit_session_file(path)

        assert report.ok
        assert report.census["protocol_rows"] == 2
        assert report.census["cancelled"] == 0

    def test_dangling_turn_writes_cancelled_placeholder(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="cancelled")
        path = _drive(
            loop,
            ctx,
            initial=_INITIAL,
            increment=[_assistant_calls(["call_1"], "{}")],
        )

        report = audit_session_file(path)

        assert report.ok
        assert report.census["cancelled"] == 1
