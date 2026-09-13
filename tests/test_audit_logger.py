"""AuditLogger 插件测试 — TurnReport 纯映射 + 数据契约 v3。

覆盖场景：
1. on_message_completed 从 TurnReport 纯映射 turn span（usage 实测、
   finish_reason 原值、注入事实、退出原因）
2. on_message_started 提供 turn 身份（W3C trace id）与真实起点
3. on_pre_invoke / on_post_invoke 配对出 tool span（span_id、耗时、status）
4. tool span 独立落盘 + 嵌套进 turn span；未配对（interrupted）标记
5. JSONL 落盘与 record_type 行判别
6. 本轮窗口内容侧字段：全部 user 输入（含注入）与 assistant 回复
7. 契约快照：turn 级字段表一一对应
8. 并发 turn 隔离
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.agent.specs import InjectionFact, IterationFact, TurnLedger, TurnReport
from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin, _SCHEMA
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.kernel.context_sandbox_var import bind_context_root, reset_context_root
from nanobee.kernel.skill_manager import SkillsLoader
from nanobee.plugins.base import PluginMetadata

_TURN_ID = "a" * 32  # W3C trace id 形态


def _make_context_pipeline(tmp_path: Path) -> ContextPipeline:
    core_md = tmp_path / "core.md"
    core_md.write_text(
        "# Test\n\n## Soul\n你是一个助手\n\n## Rules\n请遵守规则。\n",
        encoding="utf-8",
    )
    return ContextPipeline(
        core_md_path=str(core_md),
        skill_loader=SkillsLoader(tmp_path / "skills"),
    )


def _make_plugin() -> AuditLoggerPlugin:
    return AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))


def _ctx(user_id: str = "test-user") -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = user_id
    return ctx


def _report(
    messages: list[dict],
    *,
    iterations: list[IterationFact] | None = None,
    injections: list[InjectionFact] | None = None,
    turn_id: str = _TURN_ID,
    exit_reason: str = "completed",
    error: str | None = None,
    turn_started_at: str = "",
    turn_ended_at: str = "",
) -> TurnReport:
    """构造 TurnReport 测试载荷（模拟 loop 盖章 + runner 账本）。"""
    ledger = TurnLedger(
        turn_input_index=0,
        iterations=iterations or [],
        injections=injections or [],
        exit_reason=exit_reason,
        error=error,
    )
    return TurnReport(
        turn_id=turn_id,
        turn_started_at=turn_started_at or datetime.now().astimezone().isoformat(),
        ledger=ledger,
        turn_ended_at=turn_ended_at,
        messages_window=messages,
    )


class TestTurnSpan:
    """turn span 的纯映射产出与字段完整性。"""

    @pytest.mark.asyncio
    async def test_call_count_increments(self):
        """call_count 每次 on_message_completed 递增。"""
        plugin = _make_plugin()
        ctx = _ctx()

        assert plugin.call_count == 0
        await plugin.on_message_completed(ctx, _report([]))
        assert plugin.call_count == 1
        await plugin.on_message_completed(ctx, _report([]))
        assert plugin.call_count == 2

    @pytest.mark.asyncio
    async def test_turn_span_fields(self):
        """turn span 字段来自账本直通：迭代数、usage 累计、finish_reason 原值。"""
        plugin = _make_plugin()
        ctx = _ctx()

        report = _report(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
            ],
            iterations=[
                IterationFact(no=0, finish_reason="stop",
                              usage={"prompt_tokens": 10, "completion_tokens": 4}),
            ],
        )
        await plugin.on_message_started(ctx, "你好", report.turn_id)
        await plugin.on_message_completed(ctx, report)

        spans = plugin.completed_spans("test-user")
        assert len(spans) == 1
        span = spans[0]
        assert span["gen_ai.operation.name"] == "invoke_agent"
        assert span["gen_ai.conversation.id"] == "test-user"
        assert span["nanobee.messages"] == 2
        assert span["nanobee.iterations"] == 1
        assert span["gen_ai.response.finish_reasons"] == ["stop"]
        assert span["gen_ai.usage.input_tokens"] == 10
        assert span["gen_ai.usage.output_tokens"] == 4
        assert span["gen_ai.usage.total_tokens"] == 14
        assert span["duration_ms"] is not None
        assert span["schema"] == _SCHEMA
        assert span["gen_ai.agent.name"] == "nanobee"
        # v3：provider 实测口径
        assert span["nanobee.usage.estimated"] is False

    @pytest.mark.asyncio
    async def test_trace_id_from_report_is_w3c(self):
        """v3：trace_id 取框架 W3C trace id（与日志流 set_trace_id 同源）。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "你好", _TURN_ID)
        await plugin.on_message_completed(ctx, _report([]))

        span = plugin.completed_spans("test-user")[0]
        assert span["trace_id"] == _TURN_ID

    @pytest.mark.asyncio
    async def test_finish_reasons_dedup_preserve_order(self):
        """finish_reasons 为账本原值，保序去重。"""
        plugin = _make_plugin()
        ctx = _ctx()

        report = _report(
            [],
            iterations=[
                IterationFact(no=0, finish_reason="tool_calls"),
                IterationFact(no=1, finish_reason="stop"),
                IterationFact(no=2, finish_reason="stop"),
            ],
        )
        await plugin.on_message_completed(ctx, report)

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.response.finish_reasons"] == ["tool_calls", "stop"]

    @pytest.mark.asyncio
    async def test_usage_summed_across_iterations(self):
        """多轮迭代 usage 逐轮累加（provider 实测求和）。"""
        plugin = _make_plugin()
        ctx = _ctx()

        report = _report(
            [],
            iterations=[
                IterationFact(no=0, usage={"prompt_tokens": 10, "completion_tokens": 3}),
                IterationFact(no=1, usage={"prompt_tokens": 30, "completion_tokens": 7}),
            ],
        )
        await plugin.on_message_completed(ctx, report)

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.usage.input_tokens"] == 40
        assert span["gen_ai.usage.output_tokens"] == 10
        assert span["gen_ai.usage.total_tokens"] == 50
        assert span["nanobee.iterations"] == 2

    @pytest.mark.asyncio
    async def test_exit_reason_and_error_mapped(self):
        """runner 出口盖章直通：exit_reason / error 进契约。"""
        plugin = _make_plugin()
        ctx = _ctx()

        report = _report(
            [],
            exit_reason="completed",
            error="LLM 调用失败：超时",
        )
        await plugin.on_message_completed(ctx, report)

        span = plugin.completed_spans("test-user")[0]
        assert span["nanobee.exit_reason"] == "completed"
        assert span["nanobee.error"] == "LLM 调用失败：超时"

    @pytest.mark.asyncio
    async def test_injection_facts_mapped(self):
        """排空注入事实直通：次数与消息条数分开记录。"""
        plugin = _make_plugin()
        ctx = _ctx()

        report = _report(
            [],
            injections=[
                InjectionFact(count=1, phase="after tool execution"),
                InjectionFact(count=2, phase="after final response"),
            ],
        )
        await plugin.on_message_completed(ctx, report)

        span = plugin.completed_spans("test-user")[0]
        assert span["nanobee.injections"] == 2
        assert span["nanobee.injected_messages"] == 3


class TestToolSpan:
    """tool span 的配对与字段。"""

    @pytest.mark.asyncio
    async def test_pre_post_pair_produces_tool_span(self):
        """on_pre_invoke 与 on_post_invoke 配对出完整 tool span（原生 call_id）。"""
        plugin = _make_plugin()
        ctx = _ctx()

        args = await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        assert args == {"path": "a.txt"}
        result = await plugin.on_post_invoke(ctx, "call_1", "read_file", "file content")
        assert result == "file content"

        spans = plugin.tool_spans("test-user")
        assert len(spans) == 1
        span = spans[0]
        assert span["span_id"] == "call_1"
        assert span["gen_ai.tool.call.id"] == "call_1"
        assert span["gen_ai.tool.name"] == "read_file"
        assert span["duration_ms"] is not None
        assert span["gen_ai.tool.call.arguments"] != ""
        assert span["gen_ai.tool.call.result"] != ""
        assert span["status"] == "ok"
        assert span["nanobee.interrupted"] is False
        assert span["gen_ai.operation.name"] == "execute_tool"
        assert span["schema"] == _SCHEMA
        # schema 必须位于契约字段首位（与 TurnSpan 一致，便于按 key 分派）
        assert next(iter(span)) == "schema"

    @pytest.mark.asyncio
    async def test_native_call_id_correlates_concurrent_same_tool(self):
        """同工具并发时按原生 call_id 精确配对，不串扰。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_a", "read_file", {"path": "a.txt"})
        await plugin.on_pre_invoke(ctx, "call_b", "read_file", {"path": "b.txt"})
        # 乱序完成：先关 call_b，再关 call_a
        await plugin.on_post_invoke(ctx, "call_b", "read_file", "b content")
        await plugin.on_post_invoke(ctx, "call_a", "read_file", "a content")

        by_id = {s["span_id"]: s for s in plugin.tool_spans("test-user")}
        assert by_id["call_a"]["gen_ai.tool.call.result"] == "a content"
        assert by_id["call_b"]["gen_ai.tool.call.result"] == "b content"
        assert len(by_id) == 2

    @pytest.mark.asyncio
    async def test_error_result_marks_status_error(self):
        """工具返回错误文本时 status="error"。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "missing.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "Error: file not found")

        span = plugin.tool_spans("test-user")[0]
        assert span["status"] == "error"

    @pytest.mark.asyncio
    async def test_tool_span_nested_into_turn(self):
        """tool span 在 on_message_completed 时嵌套进 turn span。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "读文件", _TURN_ID)
        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
        await plugin.on_message_completed(ctx, _report([]))

        turn = plugin.completed_spans("test-user")[0]
        assert len(turn["tool_spans"]) == 1
        assert turn["tool_spans"][0]["gen_ai.tool.name"] == "read_file"

    @pytest.mark.asyncio
    async def test_unclosed_pre_invoke_marked_interrupted(self):
        """on_pre_invoke 后未配对 on_post_invoke 的 span 标记 interrupted。"""
        plugin = _make_plugin()
        ctx = _ctx()

        # 仅 pre_invoke，无 post_invoke（模拟守卫拦截/异常中断）
        await plugin.on_message_started(ctx, "写文件", _TURN_ID)
        await plugin.on_pre_invoke(ctx, "call_1", "write_file", {"path": "b.txt"})
        await plugin.on_message_completed(ctx, _report([]))

        turn = plugin.completed_spans("test-user")[0]
        span = turn["tool_spans"][0]
        assert span["nanobee.interrupted"] is True
        # interrupted 的 span status 落 "unset"
        assert span["status"] == "unset"

    @pytest.mark.asyncio
    async def test_tool_calls_equals_tool_spans_count(self):
        """tool_calls 为 hook 累计真值：与嵌套 tool_spans 数量对账一致。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "操作", _TURN_ID)
        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "ok")
        await plugin.on_pre_invoke(ctx, "call_2", "write_file", {"path": "b.txt"})
        # call_2 被守卫拦截（无 post_invoke）
        await plugin.on_message_completed(ctx, _report([]))

        turn = plugin.completed_spans("test-user")[0]
        # v3：不再被消息历史重算覆盖（守卫拦截的调用也计入）
        assert turn["nanobee.tool_calls"] == 2
        assert len(turn["tool_spans"]) == 2

    @pytest.mark.asyncio
    async def test_no_dangling_span_without_pre(self):
        """无 on_pre_invoke 直接 on_post_invoke 不产生孤儿 span。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
        await plugin.on_message_completed(ctx, _report([]))
        assert plugin.tool_spans("test-user") == []


class TestJsonlPersistence:
    """JSONL 落盘行为。"""

    @pytest.mark.asyncio
    async def test_writes_jsonl_under_context_root(self, tmp_path: Path):
        """context_root 注入时 JSONL 写入 <root>/audit_logger/<user>.jsonl。"""
        plugin = _make_plugin()
        ctx = _ctx()

        token = bind_context_root(tmp_path)
        try:
            await plugin.on_message_completed(ctx, _report([
                {"role": "user", "content": "hi"},
            ]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["record_type"] == "turn"
        assert record["gen_ai.operation.name"] == "invoke_agent"
        assert record["gen_ai.conversation.id"] == "test-user"

    @pytest.mark.asyncio
    async def test_tool_span_only_nested_in_turn_line(self, tmp_path: Path):
        """tool span 不独立落行，仅嵌套在 turn 行内（一行 = 一个终态 turn）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
            await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
            await plugin.on_message_completed(ctx, _report([]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]
        # 单行落盘且为 turn 行；tool 事实经嵌套 tool_spans 完整保留
        assert len(records) == 1
        assert records[0]["record_type"] == "turn"
        nested = records[0]["tool_spans"]
        assert len(nested) == 1
        assert nested[0]["gen_ai.tool.name"] == "read_file"
        assert nested[0]["gen_ai.tool.call.result"] == "content"
        assert nested[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_interrupted_tool_span_nested_in_turn_line(self, tmp_path: Path):
        """interrupted 的 tool span 在 turn 关闭时补齐终态，仅嵌套于 turn 行。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_pre_invoke(ctx, "call_1", "write_file", {"path": "b.txt"})
            await plugin.on_message_completed(ctx, _report([]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]
        assert len(records) == 1
        nested = records[0]["tool_spans"]
        assert len(nested) == 1
        assert nested[0]["nanobee.interrupted"] is True
        assert nested[0]["end_time"] != ""

    @pytest.mark.asyncio
    async def test_does_not_crash_without_context_root(self):
        """context_root 未注入时不抛异常（回退临时目录）。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([]))
        assert plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_audit_logger_does_not_affect_prompt(self, tmp_path: Path):
        """audit_logger 不贡献提示词内容。"""
        ctx = _ctx()
        plugin = _make_plugin()
        pipeline = _make_context_pipeline(tmp_path)

        result_with = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"}, ctx, [plugin],
        )
        result_without = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"}, ctx, [],
        )
        assert result_with == result_without


class _Cfg:
    """模拟 kernel.config（带 plugins 属性的简单对象）。"""

    def __init__(self, plugins: dict) -> None:
        self.plugins = plugins


class _Kernel:
    """模拟 NanobeeKernel（仅供 initialize 配置提取）。"""

    def __init__(self, plugins: dict) -> None:
        self.config = _Cfg(plugins)


class TestTruncationFlags:
    """截断诚实性：nanobee.arguments/result.truncated 标记与配置覆盖。"""

    @pytest.mark.asyncio
    async def test_short_args_and_results_not_truncated(self):
        """短参数与短结果不触发截断标记。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "ok")

        span = plugin.tool_spans("test-user")[0]
        assert span["nanobee.arguments.truncated"] is False
        assert span["nanobee.result.truncated"] is False

    @pytest.mark.asyncio
    async def test_long_args_marked_truncated(self):
        """超过 arg_max_chars 的参数标记 truncated=True 且预览带省略号。"""
        plugin = _make_plugin()
        ctx = _ctx()
        long_args = {"path": "a" * 3000}

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", long_args)
        span = plugin.tool_spans("test-user")[0]
        assert span["nanobee.arguments.truncated"] is True
        assert span["gen_ai.tool.call.arguments"].endswith("...")
        assert len(span["gen_ai.tool.call.arguments"]) == plugin.config.arg_max_chars + len("...")

    @pytest.mark.asyncio
    async def test_long_result_marked_truncated(self):
        """超过 result_max_chars 的结果标记 truncated=True 且预览带省略号。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "x" * 3000)

        span = plugin.tool_spans("test-user")[0]
        assert span["nanobee.result.truncated"] is True
        assert span["gen_ai.tool.call.result"].endswith("...")
        assert len(span["gen_ai.tool.call.result"]) == plugin.config.result_max_chars + len("...")

    @pytest.mark.asyncio
    async def test_jsonl_record_contains_truncation_fields(self, tmp_path: Path):
        """JSONL 记录包含截断标记字段（纯增量字段）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a" * 3000})
            await plugin.on_post_invoke(ctx, "call_1", "read_file", "x" * 3000)
            await plugin.on_message_completed(ctx, _report([]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]
        # 单行 turn 记录，嵌套 tool_spans 携带截断标记
        assert len(records) == 1
        tool_span = records[0]["tool_spans"][0]
        assert tool_span["nanobee.arguments.truncated"] is True
        assert tool_span["nanobee.result.truncated"] is True

    def test_init_defaults_without_initialize(self):
        """未调用 initialize() 时 config_cls 已持默认实例（字段默认值可用）。"""
        plugin = _make_plugin()
        assert plugin.config.arg_max_chars == 2000
        assert plugin.config.result_max_chars == 2000
        assert plugin.config.error_max_chars == 500
        assert plugin.config.agent_name == "nanobee"

    @pytest.mark.asyncio
    async def test_error_truncated_and_whitespace_folded(self):
        """nanobee.error 截断 + 空白折叠（评审 F7：防泄敏与 JSONL 断行）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        raw_error = "Error: " + "x" * 2000 + "\nline2\r\n tail"

        await plugin.on_message_completed(ctx, _report([], error=raw_error))

        stored = plugin.completed_spans("test-user")[0]["nanobee.error"]
        assert stored is not None
        assert len(stored) <= 500 + 3  # 截断上限 + 省略号
        assert "\n" not in stored and "\r" not in stored and " " not in stored

    @pytest.mark.asyncio
    async def test_preview_truncate_false_disables_truncation(self):
        """preview_truncate: false 关闭截断：全量记录且 truncated 标记为 False。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({
            "audit_logger": {"preview_truncate": False},
        }))
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a" * 3000})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "x" * 3000)

        span = plugin.tool_spans("test-user")[0]
        assert span["nanobee.arguments.truncated"] is False
        assert "a" * 3000 in span["gen_ai.tool.call.arguments"]
        assert span["nanobee.result.truncated"] is False
        assert "x" * 3000 in span["gen_ai.tool.call.result"]

    def test_non_positive_max_falls_back_to_default(self):
        """preview_truncate 开启时，非正数的 max 配置回退默认值（显式校验，无隐式语义）。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({"audit_logger": {"arg_max_chars": 0}}))
        assert plugin.config.arg_max_chars == 2000

    def test_non_numeric_max_falls_back_to_default(self):
        """非数字的 max 配置回退默认值且不抛异常。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({"audit_logger": {"result_max_chars": "abc"}}))
        assert plugin.config.result_max_chars == 2000
        assert plugin.config.arg_max_chars == 2000

    def test_initialize_reads_config_override(self):
        """initialize() 从 plugins.audit_logger 段读取截断阈值覆盖默认值。"""
        plugin = _make_plugin()
        kernel = _Kernel({
            "audit_logger": {"arg_max_chars": 5, "result_max_chars": 5},
        })
        plugin.initialize(kernel)
        assert plugin.config.arg_max_chars == 5
        assert plugin.config.result_max_chars == 5

    @pytest.mark.asyncio
    async def test_config_override_takes_effect_on_spans(self):
        """配置覆盖后的截断阈值实际作用于 span 预览。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({
            "audit_logger": {"arg_max_chars": 10},
        }))
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a" * 50})
        span = plugin.tool_spans("test-user")[0]
        assert span["nanobee.arguments.truncated"] is True
        assert len(span["gen_ai.tool.call.arguments"]) == 10 + len("...")

    @pytest.mark.asyncio
    async def test_agent_name_config_override(self):
        """agent_name 配置项覆盖默认值并出现在契约 gen_ai.agent.name 字段。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({
            "audit_logger": {"agent_name": "my-agent"},
        }))
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": "hi"},
        ]))

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.agent.name"] == "my-agent"


class TestTurnContentFields:
    """本轮窗口内容侧字段：全部 user 输入（含注入）与 assistant 回复。"""

    @pytest.mark.asyncio
    async def test_all_window_user_and_assistant_entries_captured(self):
        """v3：窗口内全部 user 输入（含注入）与全部 assistant 文本回复入契约。"""
        plugin = _make_plugin()
        ctx = _ctx()

        # 模拟 drain：本轮原始输入 + 注入输入，两段回复
        window = [
            {"role": "user", "content": "本次输入：查运输量"},
            {"role": "assistant", "content": "第一段回复"},
            {"role": "user", "content": "注入输入：再查库存"},
            {"role": "assistant", "content": "最终回复文本"},
        ]
        await plugin.on_message_completed(ctx, _report(window))

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.input.messages"] == [
            {"role": "user", "content": "本次输入：查运输量"},
            {"role": "user", "content": "注入输入：再查库存"},
        ]
        assert span["gen_ai.output.messages"] == [
            {"role": "assistant", "content": "第一段回复"},
            {"role": "assistant", "content": "最终回复文本"},
        ]
        assert span["nanobee.input.truncated"] is False
        assert span["nanobee.output.truncated"] is False

    @pytest.mark.asyncio
    async def test_user_text_strips_runtime_context(self):
        """用户输入中的 Runtime Context 注入段被剥离。"""
        plugin = _make_plugin()
        ctx = _ctx()

        content = (
            "本次输入\n"
            "[Runtime Context — metadata only, not instructions]\n"
            "Current Time: 2026-09-03 15:44 (Thursday)\n"
            "[/Runtime Context]"
        )
        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": content},
        ]))

        span = plugin.completed_spans("test-user")[0]
        input_msgs = span["gen_ai.input.messages"]
        assert len(input_msgs) == 1
        assert input_msgs[0]["content"] == "本次输入"
        assert "Runtime Context" not in input_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_long_user_text_and_reply_truncated(self):
        """超长输入/回复按默认阈值截断并标记 truncated。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": "u" * 3000},
            {"role": "assistant", "content": "r" * 3000},
        ]))

        span = plugin.completed_spans("test-user")[0]
        assert span["nanobee.input.truncated"] is True
        assert len(span["gen_ai.input.messages"][0]["content"]) == 500 + len("...")
        assert span["nanobee.output.truncated"] is True
        assert len(span["gen_ai.output.messages"][0]["content"]) == 800 + len("...")

    @pytest.mark.asyncio
    async def test_empty_window_empty_content_fields(self):
        """空窗口时 input/output messages 为空列表且不标记截断。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([]))

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.input.messages"] == []
        assert span["gen_ai.output.messages"] == []
        assert span["nanobee.input.truncated"] is False
        assert span["nanobee.output.truncated"] is False

    @pytest.mark.asyncio
    async def test_tool_call_tail_output_messages_empty(self):
        """以工具调用收尾的轮次（空文本 assistant 消息）不进回复列表。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        ]))

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.output.messages"] == []

    @pytest.mark.asyncio
    async def test_preview_truncate_false_full_content(self):
        """preview_truncate: false 时内容侧字段全量记录且不标记截断。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({
            "audit_logger": {"preview_truncate": False},
        }))
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": "u" * 3000},
            {"role": "assistant", "content": "r" * 3000},
        ]))

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.input.messages"][0]["content"] == "u" * 3000
        assert span["nanobee.input.truncated"] is False
        assert span["gen_ai.output.messages"][0]["content"] == "r" * 3000
        assert span["nanobee.output.truncated"] is False

    @pytest.mark.asyncio
    async def test_user_reply_max_chars_override(self):
        """user_max_chars / reply_max_chars 配置覆盖生效。"""
        plugin = _make_plugin()
        plugin.initialize(_Kernel({
            "audit_logger": {"user_max_chars": 10, "reply_max_chars": 20},
        }))
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "r" * 50},
        ]))

        span = plugin.completed_spans("test-user")[0]
        assert len(span["gen_ai.input.messages"][0]["content"]) == 10 + len("...")
        assert len(span["gen_ai.output.messages"][0]["content"]) == 20 + len("...")

    @pytest.mark.asyncio
    async def test_multimodal_content_recorded_as_json_preview(self):
        """非字符串 content（如多模态 parts 列表）以 JSON 文本形式记录。"""
        plugin = _make_plugin()
        ctx = _ctx()

        content = [{"type": "text", "text": "看这张图"}]
        await plugin.on_message_completed(ctx, _report([
            {"role": "user", "content": content},
        ]))

        span = plugin.completed_spans("test-user")[0]
        input_msgs = span["gen_ai.input.messages"]
        assert len(input_msgs) == 1
        assert "看这张图" in input_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_jsonl_record_contains_content_fields(self, tmp_path: Path):
        """JSONL 落盘记录包含内容侧字段（验收：grep 回复文本直接命中）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_message_completed(ctx, _report([
                {"role": "user", "content": "捏造运输量输入"},
                {"role": "assistant", "content": "运输量 30.00"},
            ]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        record = json.loads(jsonl.read_text(encoding="utf-8").strip())
        assert record["gen_ai.input.messages"][0]["content"] == "捏造运输量输入"
        assert record["gen_ai.output.messages"][0]["content"] == "运输量 30.00"


class TestIsoTimestamps:
    """ISO 墙钟时间戳（可读、可对时、可跨日志流关联）。"""

    @pytest.mark.asyncio
    async def test_turn_start_time_from_report_stamp(self):
        """v3：turn start_time 采用 loop 盖章的 dispatch 时刻。"""
        plugin = _make_plugin()
        ctx = _ctx()

        stamp = "2026-09-13T08:00:00+08:00"
        await plugin.on_message_started(ctx, "hi", _TURN_ID)
        await plugin.on_message_completed(
            ctx, _report([], turn_started_at=stamp),
        )

        span = plugin.completed_spans("test-user")[0]
        assert span["start_time"] == stamp
        end = datetime.fromisoformat(span["end_time"])
        start = datetime.fromisoformat(span["start_time"])
        assert end >= start

    @pytest.mark.asyncio
    async def test_tool_span_iso_parseable(self):
        """tool span 的 start_time 在 pre 时填充、end_time 在 post 时填充。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        span = plugin.tool_spans("test-user")[0]
        assert span["start_time"] != ""
        assert span["end_time"] == ""
        datetime.fromisoformat(span["start_time"])

        await plugin.on_post_invoke(ctx, "call_1", "read_file", "ok")
        span = plugin.tool_spans("test-user")[0]
        end = datetime.fromisoformat(span["end_time"])
        start = datetime.fromisoformat(span["start_time"])
        assert end >= start

    @pytest.mark.asyncio
    async def test_interrupted_tool_span_has_end_time(self):
        """interrupted 的 tool span 在 turn 关闭时补齐 end_time。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "write_file", {"path": "b.txt"})
        await plugin.on_message_completed(ctx, _report([]))

        turn = plugin.completed_spans("test-user")[0]
        span = turn["tool_spans"][0]
        assert span["nanobee.interrupted"] is True
        assert span["end_time"] != ""
        datetime.fromisoformat(span["end_time"])

    @pytest.mark.asyncio
    async def test_jsonl_record_contains_iso_fields(self, tmp_path: Path):
        """JSONL 落盘记录包含 ISO 墙钟字段。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_message_completed(ctx, _report([
                {"role": "user", "content": "hi"},
            ]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]
        record = next(r for r in records if r["record_type"] == "turn")
        assert "start_time" in record
        assert "end_time" in record
        assert isinstance(record["tool_spans"], list)
        # perf_counter 不再落盘：dataclass 字段中不含 _pc_start
        assert "_pc_start" not in record
        # 每一行均携带 record_type 行判别字段
        assert all("_pc_start" not in r and "record_type" in r for r in records)


class TestContractSnapshot:
    """契约快照：一条样例轮次的结构与 v3 字段表对齐。"""

    @pytest.mark.asyncio
    async def test_contract_snapshot_keys(self):
        """样例轮次的 contract dict 键与 v3 turn 级字段表一一对应。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "查一下运输量", _TURN_ID)
        await plugin.on_pre_invoke(
            ctx, "call_01J9", "query_records", {"table": "shipments"},
        )
        await plugin.on_post_invoke(
            ctx, "call_01J9", "query_records",
            "[{'records': [{'shipment': 'A1001', 'weight': '30.00'}]}]",
        )
        await plugin.on_message_completed(ctx, _report(
            [
                {"role": "user", "content": "查一下运输量"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_01J9", "name": "query_records"}]},
                {"role": "tool", "tool_call_id": "call_01J9",
                 "content": "[{'records': [{'shipment': 'A1001', 'weight': '30.00'}]}]"},
                {"role": "assistant", "content": "运输量是 30.00"},
            ],
            iterations=[
                IterationFact(no=0, finish_reason="tool_calls",
                              usage={"prompt_tokens": 5, "completion_tokens": 1}),
                IterationFact(no=1, finish_reason="stop",
                              usage={"prompt_tokens": 15, "completion_tokens": 9}),
            ],
        ))

        span = plugin.completed_spans("test-user")[0]

        # v3 turn 级字段逐一断言
        expected_keys = {
            "schema", "record_type", "trace_id",
            "gen_ai.operation.name", "gen_ai.agent.name",
            "gen_ai.conversation.id",
            "start_time", "end_time", "duration_ms",
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
            "gen_ai.usage.total_tokens", "nanobee.usage.estimated",
            "gen_ai.response.finish_reasons",
            "gen_ai.input.messages", "gen_ai.output.messages",
            "nanobee.input.truncated", "nanobee.output.truncated",
            "nanobee.iterations", "nanobee.messages", "nanobee.tool_calls",
            "nanobee.injections", "nanobee.injected_messages",
            "nanobee.exit_reason", "nanobee.error",
            "tool_spans",
        }
        assert set(span.keys()) == expected_keys

        # schema 固定
        assert span["schema"] == _SCHEMA

        # gen_ai.operation.name 固定
        assert span["gen_ai.operation.name"] == "invoke_agent"

        # finish_reasons 原值保序去重
        assert span["gen_ai.response.finish_reasons"] == ["tool_calls", "stop"]

        # usage 实测口径
        assert span["nanobee.usage.estimated"] is False
        assert span["gen_ai.usage.total_tokens"] == 30

        # 身份：W3C trace id
        assert span["trace_id"] == _TURN_ID

        # tool_spans 结构
        assert len(span["tool_spans"]) == 1
        tool_span = span["tool_spans"][0]
        assert tool_span["span_id"] == "call_01J9"
        assert tool_span["gen_ai.tool.call.id"] == "call_01J9"
        assert tool_span["gen_ai.tool.name"] == "query_records"
        assert tool_span["gen_ai.operation.name"] == "execute_tool"
        assert "shipments" in tool_span["gen_ai.tool.call.arguments"]
        assert "30.00" in tool_span["gen_ai.tool.call.result"]
        assert tool_span["status"] == "ok"
        assert tool_span["nanobee.interrupted"] is False

        # tool span 也排除 perf_counter 内部字段
        assert "_pc_start" not in tool_span

    @pytest.mark.asyncio
    async def test_contract_jsonl_write_is_direct_asdict(self, tmp_path: Path):
        """JSONL 单行记录与 contract dict 完全一致（无额外映射层）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
            await plugin.on_post_invoke(ctx, "call_1", "read_file", "file content")
            await plugin.on_message_completed(ctx, _report([
                {"role": "user", "content": "读取文件"},
                {"role": "assistant", "content": "已读取"},
            ]))
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]

        # 单行 turn 记录 = completed_spans 的 contract dict，tool 事实在嵌套中对齐
        assert len(records) == 1
        assert records[0] == plugin.completed_spans("test-user")[0]
        assert records[0]["tool_spans"][0] == plugin.tool_spans("test-user")[0]


class TestTurnStartHook:
    """on_message_started 提供真实 turn 起点与 turn 身份。"""

    @pytest.mark.asyncio
    async def test_toolless_turn_duration_is_real(self):
        """纯聊天 turn：dispatch → sleep → completed，duration_ms 反映真实耗时。

        时间口径（D3）：start = report.turn_started_at（loop dispatch 时刻，
        在 sleep 前构造），end = turn_ended_at 缺失时退化为 completed 墙钟。
        """
        plugin = _make_plugin()
        ctx = _ctx()

        # 模拟 loop dispatch 时刻盖的 started 章（先于耗时操作）
        report = _report(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！"},
            ],
            turn_started_at=datetime.now().astimezone().isoformat(),
        )
        await plugin.on_message_started(ctx, "你好", _TURN_ID)
        await asyncio.sleep(0.05)
        await plugin.on_message_completed(ctx, report)

        span = plugin._completed["test-user"][-1]
        assert span.duration_ms is not None
        assert span.duration_ms >= 40.0

    @pytest.mark.asyncio
    async def test_start_time_precedes_end_time(self):
        """start_time / end_time 墙钟时间戳单调。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "你好", _TURN_ID)
        await asyncio.sleep(0.02)
        await plugin.on_message_completed(ctx, _report([]))

        span = plugin._completed["test-user"][-1]
        start = datetime.fromisoformat(span.start_time)
        end = datetime.fromisoformat(span.end_time)
        assert start < end

    @pytest.mark.asyncio
    async def test_stale_turn_state_discarded_on_started(self):
        """未完结旧 turn 在新 turn started 时被丢弃，不并入新 span。"""
        plugin = _make_plugin()
        ctx = _ctx()

        # 模拟上一 turn 只调用了工具、从未 completed（孤儿状态）
        await plugin.on_pre_invoke(ctx, "call_old", "read_file", {"path": "a.txt"})
        assert "test-user" in plugin._turns

        # 新 turn 开始：旧状态被丢弃，建立全新 span
        await plugin.on_message_started(ctx, "新问题", _TURN_ID)
        state = plugin._turns["test-user"]
        assert state.span.tool_calls == 0
        assert state.pending == []

        # 新 turn 正常完结，旧工具调用未混入
        await plugin.on_message_completed(ctx, _report([]))
        span = plugin._completed["test-user"][-1]
        assert span.tool_calls == 0

    @pytest.mark.asyncio
    async def test_with_tools_turn_start_earlier_than_first_tool(self):
        """有工具 turn：turn 起点早于首个工具调用起点。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_started(ctx, "读文件", _TURN_ID)
        turn_start = datetime.fromisoformat(plugin._turns["test-user"].span.start_time)
        await asyncio.sleep(0.02)
        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        tool_span = plugin._turns["test-user"].span.tool_spans[0]
        tool_start = datetime.fromisoformat(tool_span.start_time)
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
        await plugin.on_message_completed(ctx, _report([]))

        assert turn_start < tool_start

    @pytest.mark.asyncio
    async def test_completed_without_started_fallback(self):
        """兜底：未触发 started 时（插件中途启用等）completed 仍可用。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, _report([]))

        assert len(plugin.completed_spans("test-user")) == 1


class TestConcurrentTurns:
    """100 并发 turn 交叉 pre/post：span_id 不重复、turn 间不串扰。

    隔离维度说明：audit_logger 按 user_id 分桶（_turns/_completed），
    不落 parent_span_id；「parent 不串」在此语境下即各 turn 的 tool
    span 只归自己的 turn 记录。同 user_id 并发多 turn 不在保证范围。
    """

    @pytest.mark.asyncio
    async def test_100_concurrent_turns_isolated_by_user(self):
        """100 个 gather turn 交叉执行后状态零串扰。"""
        plugin = _make_plugin()

        async def run_turn(i: int) -> None:
            ctx = _ctx(f"user-{i}")
            call_a, call_b = f"call_{i}_a", f"call_{i}_b"
            await plugin.on_message_started(ctx, f"q{i}", f"turn-{i}")
            await plugin.on_pre_invoke(ctx, call_a, "tool_a", {"i": i})
            await plugin.on_pre_invoke(ctx, call_b, "tool_b", {"i": i})
            # 交叉收口：先关 b 再关 a（与开启顺序相反）
            await plugin.on_post_invoke(ctx, call_b, "tool_b", f"res-{i}-b")
            await plugin.on_post_invoke(ctx, call_a, "tool_a", f"res-{i}-a")
            await plugin.on_message_completed(ctx, _report(
                [
                    {"role": "user", "content": f"q{i}"},
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"id": call_a}]},
                    {"role": "tool", "tool_call_id": call_a,
                     "content": f"res-{i}-a"},
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"id": call_b}]},
                    {"role": "tool", "tool_call_id": call_b,
                     "content": f"res-{i}-b"},
                    {"role": "assistant", "content": f"ans-{i}"},
                ],
                turn_id=f"turn-{i}",
            ))

        await asyncio.gather(*[run_turn(i) for i in range(100)])

        # 100 个 turn 全部完结，trace_id 全局唯一（W3C turn 身份透传）
        all_turns = plugin.completed_spans()
        assert len(all_turns) == 100
        trace_ids = [t["trace_id"] for t in all_turns]
        assert len(set(trace_ids)) == 100

        # 200 个 tool span，span_id 无重复
        tool_ids = [s["span_id"] for s in plugin.tool_spans()]
        assert len(tool_ids) == 200
        assert len(set(tool_ids)) == 200

        # 「parent 不串」：每个 turn 的嵌套 tool_spans 只含自己的 call_id，
        # 且结果预览与自己的 user 对应（无他人数据混入）
        for i in range(100):
            turn = plugin.completed_spans(f"user-{i}")[0]
            ids = {s["span_id"] for s in turn["tool_spans"]}
            assert ids == {f"call_{i}_a", f"call_{i}_b"}
            results = {
                s["gen_ai.tool.call.result"] for s in turn["tool_spans"]
            }
            assert results == {f"res-{i}-a", f"res-{i}-b"}
            # 无未配对 interrupted 残留；tool_calls 为 hook 真值
            assert turn["nanobee.tool_calls"] == 2
            assert all(s["status"] == "ok" for s in turn["tool_spans"])


class TestErrorRedaction:
    """评审建议 #5：error 落盘前脱敏（先脱敏再截断，键名保留）。"""

    def test_plugin_reuses_shared_redactor(self):
        """插件不持私有正则：落盘脱敏复用 utils.redact.redact_secrets。"""
        from nanobee.builtin.audit_logger import plugin as audit_plugin
        from nanobee.utils.redact import redact_secrets

        assert audit_plugin.redact_secrets is redact_secrets

    @pytest.mark.asyncio
    async def test_url_query_key_redacted(self):
        """URL 鉴权密钥（httpx 异常典型形态）值被掩码，键名保留。"""
        plugin = _make_plugin()
        ctx = _ctx()
        secret = "fakekeyfakekeyfakekey"
        report = _report(
            [],
            error=(
                "HTTPStatusError: 500 Internal Server Error for url "
                f"'https://mcp-gw.example.com/server/abc?key={secret}'"
            ),
        )
        await plugin.on_message_completed(ctx, report)

        err = plugin.completed_spans("test-user")[0]["nanobee.error"]
        assert secret not in err
        assert "<redacted>" in err
        assert "key=" in err

    @pytest.mark.asyncio
    async def test_authorization_header_redacted(self):
        """Authorization 头形态同样掩码（含 Bearer 前缀）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        report = _report(
            [],
            error="request failed with Authorization: Bearer sk-ant-api-xyz123",
        )
        await plugin.on_message_completed(ctx, report)

        err = plugin.completed_spans("test-user")[0]["nanobee.error"]
        assert "sk-ant-api-xyz123" not in err
        assert "<redacted>" in err

    @pytest.mark.asyncio
    async def test_redaction_applied_before_truncation(self):
        """凭证位于截断边界之后也必须被掩码（先脱敏再截断）。"""
        from nanobee.builtin.audit_logger.plugin import AuditLoggerConfig

        plugin = _make_plugin()
        # config 为只读 property（底层 _config 由 PluginManager 注入），测试直接注入
        plugin._config = AuditLoggerConfig(error_max_chars=50)
        ctx = _ctx()
        secret = "supersecret-value-9876543210"
        report = _report(
            [],
            error="x" * 200 + f" failed with api_key={secret}",
        )
        await plugin.on_message_completed(ctx, report)

        err = plugin.completed_spans("test-user")[0]["nanobee.error"]
        assert len(err) <= 50 + len("<redacted>") + 10  # 截断 + 掩码标记余量
        assert secret not in err
