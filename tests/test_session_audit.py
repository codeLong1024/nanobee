"""会话文件协议契约校验器（``nanobee/session/session_audit.py``）测试。

覆盖三类：
1. 六类违规（V1..V6）各一例 + 合法序列零违规；
2. 计数普查、字节/角色聚合、文件入口与 CLI 退出码；
3. **交叉一致性断言防漂移**：合法序列同时满足"校验器零违规""窗口头部合法
   （``find_legal_message_start == 0``）""窗口尾部合法
   （``find_legal_message_end == len(messages)``）"，且校验器与 loop 侧两个落盘
   标记常量逐字相等。任一处实现漂移即红。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanobee.agent.loop import (
    _CANCELLED_TOOL_RESULT_CONTENT,
    _PERSIST_TRUNCATED_SUFFIX,
    AgentLoop,
    TurnContext,
    TurnState,
)
from nanobee.agent.messages import InboundMessage
from nanobee.session.session_audit import (
    CANCELLED_TOOL_RESULT_CONTENT,
    PERSIST_TRUNCATED_SUFFIX,
    audit_messages,
    audit_session_file,
    main,
    scan_session_dir,
)
from nanobee.session.session_manager import SessionManager
from nanobee.utils.helpers import find_legal_message_end, find_legal_message_start


def _system(content: str = "sys") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str = "hi") -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant_call(
    call_ids: list[str],
    *,
    name: str = "cron_create",
    arguments: str = '{"cron": "0 9 * * *"}',
) -> dict[str, Any]:
    return {
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


def _tool_result(
    call_id: str,
    content: str = "ok",
    *,
    name: str = "cron_create",
) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def _assistant_text(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _legal_transcript() -> list[dict[str, Any]]:
    """一段完整合法序列（user → 声明 → 结果 → 终文本）。"""
    return [
        _user("建个定时任务"),
        _assistant_call(["call_1"]),
        _tool_result("call_1"),
        _assistant_text("已创建"),
    ]


def _write_session(path: Path, messages: list[Any]) -> Path:
    """按 SessionStore.save 的形态（首行元数据 + 逐行 JSON）落一个会话文件。"""
    meta = {
        "_type": "metadata",
        "session_id": "test:c1",
        "user_id": "u1",
        "message_count": len(messages),
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines += [json.dumps(message, ensure_ascii=False) for message in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _StubProvider:
    """只提供默认模型名（够最小 AgentLoop 使用）。"""

    def get_default_model(self) -> str:
        return "stub-model"


def _new_loop(
    tmp_path: Path,
    *,
    persist: bool = True,
    result_chars: int = 8192,
    args_chars: int = 8192,
) -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop.session_manager = SessionManager(tmp_path / "users")
    loop.event_bus = None
    loop._persist_tool_traces = persist
    loop._persist_reasoning = False
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
    return ctx


def _drive(
    loop: AgentLoop,
    ctx: TurnContext,
    *,
    initial: list[dict[str, Any]],
    increment: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ctx.initial_messages = list(initial)
    ctx.all_messages = [*initial, *increment]
    asyncio.run(loop._state_save(ctx))
    return loop.session_manager.get_or_create(ctx.context_id, ctx.session_id).messages


def _codes(report: Any) -> set[str]:
    return {violation.code for violation in report.violations}


class TestViolations:
    """六类违规各一例。"""

    def test_unknown_role_flagged(self) -> None:
        report = audit_messages([_user("hi"), {"role": "bogus", "content": "?"}])

        assert _codes(report) == {"V1"}
        assert report.violations[0].index == 1

    def test_non_dict_message_flagged(self) -> None:
        report = audit_messages([_user("hi"), "not-a-dict"])

        assert _codes(report) == {"V1"}
        assert report.violations[0].index == 1

    def test_empty_tool_calls_flagged(self) -> None:
        report = audit_messages([{"role": "assistant", "content": None, "tool_calls": []}])

        assert _codes(report) == {"V2"}

    @pytest.mark.parametrize("raw", [5, 5.0, True, None, {"id": "c1"}, "c1"])
    def test_non_list_tool_calls_flagged_not_crash(self, raw: Any) -> None:
        """标量/对象型 tool_calls 必须报 V2，绝不能抛异常打断整次审计。

        会话文件是不可信输入；若此处 TypeError 冒泡，`scan_session_dir` 只会看到
        OSError 未捕获异常，一个损坏文件就能让整次目录扫描中止、已得报告全丢。
        """
        report = audit_messages([{"role": "assistant", "content": None, "tool_calls": raw}])

        assert _codes(report) == {"V2"}

    def test_non_list_tool_calls_does_not_break_later_rows(self) -> None:
        """前一行 tool_calls 畸形时，后续行的违规仍须被完整报出。"""
        report = audit_messages([
            {"role": "assistant", "content": None, "tool_calls": 5},
            _tool_result("orphan"),
        ])

        assert _codes(report) == {"V2", "V4"}

    def test_call_without_id_flagged(self) -> None:
        report = audit_messages([
            {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "t"}}]},
        ])

        assert _codes(report) == {"V2"}

    def test_call_with_non_string_id_flagged(self) -> None:
        report = audit_messages([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": 7, "function": {"name": "t", "arguments": "{}"}}],
            },
        ])

        assert _codes(report) == {"V2"}

    def test_tool_without_id_flagged(self) -> None:
        report = audit_messages([{"role": "tool", "content": "no id"}])

        assert _codes(report) == {"V3"}

    def test_orphan_result_flagged(self) -> None:
        report = audit_messages([_user("hi"), _tool_result("call_x")])

        assert _codes(report) == {"V4"}
        assert report.violations[0].index == 1

    def test_dangling_declaration_flagged(self) -> None:
        report = audit_messages([_assistant_call(["call_1"])])

        assert _codes(report) == {"V5"}

    def test_dangling_repaired_by_placeholder_is_clean(self) -> None:
        """悬尾声明 + 取消占位 → 合法（落盘侧悬尾修复的产物必须判为合法）。"""
        report = audit_messages([
            _assistant_call(["call_1"]),
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "cron_create",
                "content": CANCELLED_TOOL_RESULT_CONTENT,
            },
        ])

        assert report.ok
        assert report.census["cancelled"] == 1

    def test_duplicate_result_flagged(self) -> None:
        report = audit_messages([
            _assistant_call(["call_1"]),
            _tool_result("call_1", "first"),
            _tool_result("call_1", "second"),
        ])

        assert _codes(report) == {"V6"}


class TestLegalShapes:
    """合法序列零违规（存量纯文本历史与协议历史都要覆盖）。"""

    def test_legal_transcript_clean(self) -> None:
        report = audit_messages(_legal_transcript())

        assert report.ok
        assert report.counts_by_code() == {}
        assert report.message_count == 4

    def test_pure_text_history_clean(self) -> None:
        """开关关闭时的存量形态（只有 user/assistant 文本）必须零违规。"""
        report = audit_messages([
            _system(),
            _user("hi"),
            _assistant_text("hello"),
            _user("again"),
            _assistant_text("world"),
        ])

        assert report.ok
        assert report.census["protocol_rows"] == 0

    def test_multiple_groups_and_partial_batch_clean(self) -> None:
        report = audit_messages([
            _assistant_call(["c1", "c2"]),
            _tool_result("c1"),
            _tool_result("c2"),
            _assistant_call(["c3"]),
            _tool_result("c3"),
        ])

        assert report.ok


class TestCensus:
    """计数普查：是观察量，不参与合法性判定。"""

    def test_census_counts_protocol_truncated_cancelled_reasoning(self) -> None:
        report = audit_messages([
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "内部推理",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "t", "arguments": "a" * 10}},
                    {"id": "c2", "function": {"name": "t", "arguments": "b" * 10}},
                ],
            },
            _tool_result("c1", "01234" + PERSIST_TRUNCATED_SUFFIX),
            {"role": "tool", "tool_call_id": "c2", "content": CANCELLED_TOOL_RESULT_CONTENT},
            _assistant_text("done"),
        ])

        assert report.ok
        census = report.census
        assert census["protocol_rows"] == 3
        assert census["truncated"] == 1
        assert census["cancelled"] == 1
        assert census["reasoning_left"] == 1

    def test_unredacted_detected(self) -> None:
        report = audit_messages([_user("api_key=sk-super-secret")])

        assert report.census["unredacted"] == 1
        # 普查量不是违规：终文本/用户原文不走脱敏属已登记口径
        assert report.ok

    def test_masked_text_not_counted(self) -> None:
        report = audit_messages([_user("api_key=<redacted>")])

        assert report.census["unredacted"] == 0

    def test_unparsable_lines_counted(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.jsonl"
        path.write_text(
            json.dumps({"_type": "metadata", "session_id": "s"}, ensure_ascii=False)
            + "\n"
            + "{not json}\n"
            + json.dumps(_assistant_text("hi"), ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        report = audit_session_file(path)

        assert report.census["unparsable_lines"] == 1
        assert report.message_count == 1
        assert report.ok


class TestBytesAndTokens:
    """字节与 token 度量。"""

    def test_bytes_by_role_matches_independent_serialization(self) -> None:
        """按角色分桶必须等于"逐行 JSON + 换行 + UTF-8"的独立复算（非自证断言）。"""
        messages = _legal_transcript()
        report = audit_messages(messages)

        expected: dict[str, int] = {}
        for message in messages:
            role = str(message["role"])
            line = json.dumps(message, ensure_ascii=False) + "\n"
            expected[role] = expected.get(role, 0) + len(line.encode("utf-8"))

        assert report.bytes_by_role == expected
        assert sum(expected.values()) == report.bytes_total

    def test_file_bytes_use_real_file_size(self, tmp_path: Path) -> None:
        path = _write_session(tmp_path / "s.jsonl", _legal_transcript())

        report = audit_session_file(path)

        assert report.bytes_total == path.stat().st_size
        assert report.bytes_total > sum(report.bytes_by_role.values())

    def test_token_estimate_positive(self) -> None:
        report = audit_messages(_legal_transcript())

        assert report.token_estimate > 0


class TestFileEntry:
    """文件与目录入口、CLI 退出码。"""

    def test_audit_session_file_skips_metadata_line(self, tmp_path: Path) -> None:
        path = _write_session(tmp_path / "s.jsonl", _legal_transcript())

        report = audit_session_file(path)

        assert report.message_count == 4
        assert report.path == str(path)
        assert report.ok

    def test_scan_session_dir_skips_consolidation_and_tmp(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "a.jsonl", _legal_transcript())
        _write_session(tmp_path / "b.consolidation.jsonl", [{"summary": "x"}])
        _write_session(tmp_path / "c.jsonl.tmp", [_tool_result("orphan")])

        reports = scan_session_dir(tmp_path)

        assert [Path(report.path).name for report in reports] == ["a.jsonl"]
        assert all(report.ok for report in reports)

    def test_leading_blank_line_before_metadata(self, tmp_path: Path) -> None:
        """元数据行按"首个可解析行"判定，不受前导空行影响（否则会误报 V1）。"""
        path = _write_session(tmp_path / "s.jsonl", _legal_transcript())
        path.write_text("\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

        report = audit_session_file(path)

        assert report.ok
        assert report.message_count == 4

    def test_non_object_json_line_flagged(self, tmp_path: Path) -> None:
        """合法 JSON 但非对象的行要显式报出（存储层对它是静默丢弃）。"""
        path = _write_session(tmp_path / "s.jsonl", [123, _tool_result("orphan")])

        report = audit_session_file(path)

        assert _codes(report) == {"V1", "V4"}

    def test_u2028_in_content_does_not_split_line(self, tmp_path: Path) -> None:
        """U+2028 一类字符不能被当作换行：splitlines 会切断消息行造成静默漏报。"""
        path = _write_session(
            tmp_path / "s.jsonl",
            [_user("a\u2028b"), _assistant_text("ok")],
        )

        report = audit_session_file(path)

        assert report.message_count == 2
        assert report.census["unparsable_lines"] == 0
        assert report.ok

    def test_cli_exit_code_zero_for_legal(self, tmp_path: Path, capsys: Any) -> None:
        path = _write_session(tmp_path / "s.jsonl", _legal_transcript())

        assert main([str(path)]) == 0
        assert "[OK]" in capsys.readouterr().out

    def test_cli_exit_code_one_for_violation(self, tmp_path: Path, capsys: Any) -> None:
        path = _write_session(tmp_path / "s.jsonl", [_tool_result("orphan")])

        assert main([str(path)]) == 1
        output = capsys.readouterr().out
        assert "V4" in output
        assert "orphan" in output

    def test_cli_exit_code_one_for_missing_path(self, tmp_path: Path, capsys: Any) -> None:
        assert main([str(tmp_path / "nope.jsonl")]) == 1
        assert "路径不存在" in capsys.readouterr().err

    def test_cli_reports_directory(self, tmp_path: Path, capsys: Any) -> None:
        _write_session(tmp_path / "s.jsonl", _legal_transcript())

        assert main([str(tmp_path)]) == 0
        assert "files=1" in capsys.readouterr().out


class TestCrossConsistency:
    """交叉一致性断言：防"写侧语义"与"校验器判据"各自漂移。"""

    def test_marker_constants_match_loop(self) -> None:
        """标记常量与落盘侧逐字相等（session 层不能 import agent 层）。"""
        assert PERSIST_TRUNCATED_SUFFIX == _PERSIST_TRUNCATED_SUFFIX
        assert CANCELLED_TOOL_RESULT_CONTENT == _CANCELLED_TOOL_RESULT_CONTENT

    def test_audit_agrees_with_window_helpers_on_legal_transcript(self) -> None:
        messages = _legal_transcript()

        report = audit_messages(messages)

        assert report.ok
        assert find_legal_message_start(messages) == 0
        assert find_legal_message_end(messages) == len(messages)

    @pytest.mark.parametrize(
        ("messages", "code"),
        [
            ([_tool_result("call_x")], "V4"),
            ([_assistant_call(["call_1"])], "V5"),
            ([_tool_result("call_x"), _assistant_call(["call_1"])], "V5"),
        ],
    )
    def test_audit_flags_what_window_helpers_repair(
        self,
        messages: list[dict[str, Any]],
        code: str,
    ) -> None:
        """窗口修复函数要裁掉的东西，校验器必须报出来（两套判据同向）。"""
        report = audit_messages(messages)

        assert code in _codes(report)
        if code == "V4":
            assert find_legal_message_start(messages) > 0
        else:
            assert find_legal_message_end(messages) < len(messages)


class TestRealSaveOutput:
    """落盘产物必须天然合法（校验器用于验收的前提）。"""

    def test_saved_session_file_is_clean(self, tmp_path: Path) -> None:
        loop = _new_loop(tmp_path, result_chars=4, args_chars=4)
        ctx = _make_ctx(final_content="已创建")
        _drive(
            loop,
            ctx,
            initial=[_system(), _user("建个定时任务")],
            increment=[
                _assistant_call(["call_1"], arguments='{"api_key": "sk-secret"}'),
                _tool_result("call_1", "0123456789"),
                _assistant_text("已创建"),
            ],
        )

        path = loop.session_manager.store._session_path("u1", "test:c1")
        report = audit_session_file(path)

        assert report.ok
        assert report.census["protocol_rows"] == 2
        assert report.census["truncated"] == 2
        assert report.census["unredacted"] == 0

    def test_dangling_turn_saved_as_clean(self, tmp_path: Path) -> None:
        """悬尾轮（守卫拦截 / 中断）落盘后同样合法。"""
        loop = _new_loop(tmp_path)
        ctx = _make_ctx(error="cancelled")
        _drive(
            loop,
            ctx,
            initial=[_system(), _user("hi")],
            increment=[_assistant_call(["call_1"])],
        )

        path = loop.session_manager.store._session_path("u1", "test:c1")
        report = audit_session_file(path)

        assert report.ok
        assert report.census["cancelled"] == 1
