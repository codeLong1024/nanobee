"""AuditLogger 插件测试 — OTel GenAI 数据契约 v1。

覆盖场景：
1. on_message_completed 产出 turn span（计数递增、token 汇总、finish_reason）
2. on_pre_invoke / on_post_invoke 配对出 tool span（span_id、耗时、status）
3. tool span 嵌套进 turn span
4. 未配对（interrupted）的 tool span 在 turn 关闭时被标记
5. JSONL 落盘（context_root 注入时写 <root>/audit_logger/<user>.jsonl）
6. 不影响 prompt 内容
7. turn 内容侧字段：gen_ai.input/output.messages（含 Runtime Context 剥离与截断）
8. ISO 墙钟时间戳：start_time / end_time
9. 契约快照：一条样例轮次 vs §5 示例结构
10. agent_name 配置项
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin, _SCHEMA
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.kernel.context_sandbox_var import bind_context_root, reset_context_root
from nanobee.kernel.skill_manager import SkillsLoader
from nanobee.plugins.base import PluginMetadata


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


class TestTurnSpan:
    """turn span 的产出与字段完整性。"""

    @pytest.mark.asyncio
    async def test_call_count_increments(self):
        """call_count 每次 on_message_completed 递增。"""
        plugin = _make_plugin()
        ctx = _ctx()

        assert plugin.call_count == 0
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 1
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 2

    @pytest.mark.asyncio
    async def test_turn_span_fields(self):
        """turn span 包含消息数、迭代、token 汇总、finish_reason。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        await plugin.on_message_completed(ctx, messages)

        spans = plugin.completed_spans("test-user")
        assert len(spans) == 1
        span = spans[0]
        assert span["gen_ai.operation.name"] == "invoke_agent"
        assert span["gen_ai.conversation.id"] == "test-user"
        assert span["nanobee.messages"] == 2
        assert span["nanobee.iterations"] == 1
        assert span["gen_ai.response.finish_reasons"] == ["stop"]
        assert span["gen_ai.usage.output_tokens"] >= 0
        assert span["duration_ms"] is not None
        assert span["schema"] == _SCHEMA
        assert span["trace_id"].startswith("turn_")
        assert span["gen_ai.agent.name"] == "nanobee"
        assert span["nanobee.usage.estimated"] is True

    @pytest.mark.asyncio
    async def test_turn_span_finish_reason_tool_calls(self):
        """最后一条 assistant 消息带 tool_calls 时 finish_reason=["tool_calls"]。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        ]
        await plugin.on_message_completed(ctx, messages)
        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.response.finish_reasons"] == ["tool_calls"]

    @pytest.mark.asyncio
    async def test_counts_tool_calls(self):
        """nanobee.tool_calls 计数在 turn span 中正确反映。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "结果"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2"}, {"id": "call_3"}]},
        ]
        await plugin.on_message_completed(ctx, messages)
        span = plugin.completed_spans("test-user")[0]
        assert span["nanobee.tool_calls"] == 2


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

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
        await plugin.on_message_completed(ctx, [])

        turn = plugin.completed_spans("test-user")[0]
        assert len(turn["tool_spans"]) == 1
        assert turn["tool_spans"][0]["gen_ai.tool.name"] == "read_file"

    @pytest.mark.asyncio
    async def test_unclosed_pre_invoke_marked_interrupted(self):
        """on_pre_invoke 后未配对 on_post_invoke 的 span 标记 interrupted。"""
        plugin = _make_plugin()
        ctx = _ctx()

        # 仅 pre_invoke，无 post_invoke（模拟守卫拦截/异常中断）
        await plugin.on_pre_invoke(ctx, "call_1", "write_file", {"path": "b.txt"})
        await plugin.on_message_completed(ctx, [])

        turn = plugin.completed_spans("test-user")[0]
        span = turn["tool_spans"][0]
        assert span["nanobee.interrupted"] is True
        # interrupted 的 span status 落 "unset"
        assert span["status"] == "unset"

    @pytest.mark.asyncio
    async def test_no_dangling_span_without_pre(self):
        """无 on_pre_invoke 直接 on_post_invoke 不产生孤儿 span。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_post_invoke(ctx, "call_1", "read_file", "content")
        await plugin.on_message_completed(ctx, [])
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
            await plugin.on_message_completed(ctx, [{"role": "user", "content": "hi"}])
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["gen_ai.operation.name"] == "invoke_agent"
        assert record["gen_ai.conversation.id"] == "test-user"

    @pytest.mark.asyncio
    async def test_does_not_crash_without_context_root(self):
        """context_root 未注入时不抛异常（回退临时目录）。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, [])
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
            await plugin.on_message_completed(ctx, [])
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        record = json.loads(jsonl.read_text(encoding="utf-8").strip())
        tool_span = record["tool_spans"][0]
        assert tool_span["nanobee.arguments.truncated"] is True
        assert tool_span["nanobee.result.truncated"] is True

    def test_init_defaults_without_initialize(self):
        """未调用 initialize() 时 config_cls 已持默认实例（字段默认值可用）。"""
        plugin = _make_plugin()
        assert plugin.config.arg_max_chars == 2000
        assert plugin.config.result_max_chars == 2000
        assert plugin.config.agent_name == "nanobee"

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

        await plugin.on_message_completed(ctx, [{"role": "user", "content": "hi"}])

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.agent.name"] == "my-agent"


class TestTurnContentFields:
    """turn 记录的用户输入原文与最终回复预览（gen_ai.input/output.messages）。"""

    @pytest.mark.asyncio
    async def test_input_output_messages_captured(self):
        """取最后一条 user / assistant 消息作为本轮输入与最终回复。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "user", "content": "历史输入"},
            {"role": "assistant", "content": "历史回复"},
            {"role": "user", "content": "本次输入：查运输量"},
            {"role": "assistant", "content": "最终回复文本"},
        ]
        await plugin.on_message_completed(ctx, messages)

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.input.messages"] == [
            {"role": "user", "content": "本次输入：查运输量"},
        ]
        assert span["gen_ai.output.messages"] == [
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
        await plugin.on_message_completed(ctx, [{"role": "user", "content": content}])

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

        messages = [
            {"role": "user", "content": "u" * 3000},
            {"role": "assistant", "content": "r" * 3000},
        ]
        await plugin.on_message_completed(ctx, messages)

        span = plugin.completed_spans("test-user")[0]
        assert span["nanobee.input.truncated"] is True
        assert len(span["gen_ai.input.messages"][0]["content"]) == 500 + len("...")
        assert span["nanobee.output.truncated"] is True
        assert len(span["gen_ai.output.messages"][0]["content"]) == 800 + len("...")

    @pytest.mark.asyncio
    async def test_empty_messages_empty_content_fields(self):
        """空消息列表时 input/output messages 为空列表且不标记截断。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, [])

        span = plugin.completed_spans("test-user")[0]
        assert span["gen_ai.input.messages"] == []
        assert span["gen_ai.output.messages"] == []
        assert span["nanobee.input.truncated"] is False
        assert span["nanobee.output.truncated"] is False

    @pytest.mark.asyncio
    async def test_tool_call_tail_output_messages_empty(self):
        """以工具调用收尾的轮次（无最终回复文本）输出消息为空列表。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        ]
        await plugin.on_message_completed(ctx, messages)

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

        messages = [
            {"role": "user", "content": "u" * 3000},
            {"role": "assistant", "content": "r" * 3000},
        ]
        await plugin.on_message_completed(ctx, messages)

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

        messages = [
            {"role": "user", "content": "u" * 50},
            {"role": "assistant", "content": "r" * 50},
        ]
        await plugin.on_message_completed(ctx, messages)

        span = plugin.completed_spans("test-user")[0]
        assert len(span["gen_ai.input.messages"][0]["content"]) == 10 + len("...")
        assert len(span["gen_ai.output.messages"][0]["content"]) == 20 + len("...")

    @pytest.mark.asyncio
    async def test_multimodal_content_recorded_as_json_preview(self):
        """非字符串 content（如多模态 parts 列表）以 JSON 文本形式记录。"""
        plugin = _make_plugin()
        ctx = _ctx()

        content = [{"type": "text", "text": "看这张图"}]
        await plugin.on_message_completed(ctx, [{"role": "user", "content": content}])

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
            await plugin.on_message_completed(ctx, [
                {"role": "user", "content": "捏造运输量输入"},
                {"role": "assistant", "content": "运输量 30.00"},
            ])
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        record = json.loads(jsonl.read_text(encoding="utf-8").strip())
        assert record["gen_ai.input.messages"][0]["content"] == "捏造运输量输入"
        assert record["gen_ai.output.messages"][0]["content"] == "运输量 30.00"


class TestIsoTimestamps:
    """ISO 墙钟时间戳（可读、可对时、可跨日志流关联）。"""

    @pytest.mark.asyncio
    async def test_turn_span_iso_parseable_and_ordered(self):
        """turn span 的 start_time / end_time 可解析且时序正确。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_message_completed(ctx, [{"role": "user", "content": "hi"}])

        span = plugin.completed_spans("test-user")[0]
        start = datetime.fromisoformat(span["start_time"])
        end = datetime.fromisoformat(span["end_time"])
        assert end >= start
        assert span["start_time"] != ""
        assert span["end_time"] != ""

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
        await plugin.on_message_completed(ctx, [])

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
            await plugin.on_message_completed(ctx, [{"role": "user", "content": "hi"}])
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        record = json.loads(jsonl.read_text(encoding="utf-8").strip())
        assert "start_time" in record
        assert "end_time" in record
        assert isinstance(record["tool_spans"], list)
        # perf_counter 不再落盘：dataclass 字段中不含 _pc_start
        assert "_pc_start" not in record


class TestContractSnapshot:
    """契约快照：一条样例轮次的结构与 §5 示例对齐。"""

    @pytest.mark.asyncio
    async def test_contract_snapshot_keys(self):
        """样例轮次的 contract dict 键与 §3 turn 级字段表一一对应。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(
            ctx, "call_01J9", "query_records", {"table": "shipments"},
        )
        await plugin.on_post_invoke(
            ctx, "call_01J9", "query_records",
            "[{'records': [{'shipment': 'A1001', 'weight': '30.00'}]}]",
        )
        await plugin.on_message_completed(ctx, [
            {"role": "user", "content": "查一下运输量"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_01J9", "name": "query_records"}]},
            {"role": "tool", "tool_call_id": "call_01J9",
             "content": "[{'records': [{'shipment': 'A1001', 'weight': '30.00'}]}]"},
            {"role": "assistant", "content": "运输量是 30.00"},
        ])

        span = plugin.completed_spans("test-user")[0]

        # §3 turn 级字段逐一断言
        expected_keys = {
            "schema", "trace_id",
            "gen_ai.operation.name", "gen_ai.agent.name",
            "gen_ai.conversation.id",
            "start_time", "end_time", "duration_ms",
            "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
            "gen_ai.usage.total_tokens", "nanobee.usage.estimated",
            "gen_ai.response.finish_reasons",
            "gen_ai.input.messages", "gen_ai.output.messages",
            "nanobee.input.truncated", "nanobee.output.truncated",
            "nanobee.iterations", "nanobee.messages", "nanobee.tool_calls",
            "tool_spans",
        }
        assert set(span.keys()) == expected_keys

        # schema 固定
        assert span["schema"] == _SCHEMA

        # gen_ai.operation.name 固定
        assert span["gen_ai.operation.name"] == "invoke_agent"

        # finish_reasons 值域对齐 OTel
        assert span["gen_ai.response.finish_reasons"] == ["stop"]

        # usage 估算标记
        assert span["nanobee.usage.estimated"] is True

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
        """JSONL 写入的记录与 contract dict 完全一致（无额外映射层）。"""
        plugin = _make_plugin()
        ctx = _ctx()
        token = bind_context_root(tmp_path)
        try:
            await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
            await plugin.on_post_invoke(ctx, "call_1", "read_file", "file content")
            await plugin.on_message_completed(ctx, [
                {"role": "user", "content": "读取文件"},
                {"role": "assistant", "content": "已读取"},
            ])
        finally:
            reset_context_root(token)

        jsonl = tmp_path / "audit_logger" / "test-user.jsonl"
        record = json.loads(jsonl.read_text(encoding="utf-8").strip())

        # JSONL 写入记录 = completed_spans()[0] 返回的 contract dict
        assert record == plugin.completed_spans("test-user")[0]
