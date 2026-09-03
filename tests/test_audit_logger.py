"""AuditLogger 插件测试 — turn/tool 两级 span 结构化审计。

覆盖场景：
1. on_message_completed 产出 turn span（计数递增、token 汇总、finish_reason）
2. on_pre_invoke / on_post_invoke 配对出 tool span（callId、耗时、isError）
3. tool span 嵌套进 turn span
4. 未配对（interrupted）的 tool span 在 turn 关闭时被标记
5. JSONL 落盘（context_root 注入时写 <root>/audit_logger/<user>.jsonl）
6. 不影响 prompt 内容
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin
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
        assert span["type"] == "turn_span"
        assert span["user_id"] == "test-user"
        assert span["messages"] == 2
        assert span["iterations"] == 1
        assert span["finish_reason"] == "completed"
        assert span["completion_tokens"] >= 0
        assert span["duration_ms"] is not None

    @pytest.mark.asyncio
    async def test_turn_span_finish_reason_tool_call(self):
        """最后一条 assistant 消息带 tool_calls 时 finish_reason=tool_call。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        ]
        await plugin.on_message_completed(ctx, messages)
        span = plugin.completed_spans("test-user")[0]
        assert span["finish_reason"] == "tool_call"

    @pytest.mark.asyncio
    async def test_counts_tool_calls(self):
        """tool_calls 计数在 turn span 中正确反映。"""
        plugin = _make_plugin()
        ctx = _ctx()

        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "结果"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2"}, {"id": "call_3"}]},
        ]
        await plugin.on_message_completed(ctx, messages)
        span = plugin.completed_spans("test-user")[0]
        assert span["tool_calls"] == 2


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
        assert span["call_id"] == "call_1"
        assert span["tool_name"] == "read_file"
        assert span["duration_ms"] is not None
        assert span["arg_preview"] != ""
        assert span["result_preview"] != ""
        assert span["is_error"] is False
        assert span["interrupted"] is False

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

        by_id = {s["call_id"]: s for s in plugin.tool_spans("test-user")}
        assert by_id["call_a"]["result_preview"] == "a content"
        assert by_id["call_b"]["result_preview"] == "b content"
        assert len(by_id) == 2

    @pytest.mark.asyncio
    async def test_error_result_marks_is_error(self):
        """工具返回错误文本时 is_error=True。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "missing.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "Error: file not found")

        span = plugin.tool_spans("test-user")[0]
        assert span["is_error"] is True

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
        assert turn["tool_spans"][0]["tool_name"] == "read_file"

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
        assert span["interrupted"] is True

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
        assert record["type"] == "turn_span"
        assert record["user_id"] == "test-user"

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
    """截断诚实性：arg_truncated / result_truncated 标记与配置覆盖。"""

    @pytest.mark.asyncio
    async def test_short_args_and_results_not_truncated(self):
        """短参数与短结果不触发截断标记。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "ok")

        span = plugin.tool_spans("test-user")[0]
        assert span["arg_truncated"] is False
        assert span["result_truncated"] is False

    @pytest.mark.asyncio
    async def test_long_args_marked_truncated(self):
        """超过 arg_max_chars 的参数标记 arg_truncated=True 且预览带省略号。"""
        plugin = _make_plugin()
        ctx = _ctx()
        long_args = {"path": "a" * 3000}

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", long_args)
        span = plugin.tool_spans("test-user")[0]
        assert span["arg_truncated"] is True
        assert span["arg_preview"].endswith("...")
        assert len(span["arg_preview"]) == plugin.config.arg_max_chars + len("...")

    @pytest.mark.asyncio
    async def test_long_result_marked_truncated(self):
        """超过 result_max_chars 的结果标记 result_truncated=True 且预览带省略号。"""
        plugin = _make_plugin()
        ctx = _ctx()

        await plugin.on_pre_invoke(ctx, "call_1", "read_file", {"path": "a.txt"})
        await plugin.on_post_invoke(ctx, "call_1", "read_file", "x" * 3000)

        span = plugin.tool_spans("test-user")[0]
        assert span["result_truncated"] is True
        assert span["result_preview"].endswith("...")
        assert len(span["result_preview"]) == plugin.config.result_max_chars + len("...")

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
        assert tool_span["arg_truncated"] is True
        assert tool_span["result_truncated"] is True

    def test_init_defaults_without_initialize(self):
        """未调用 initialize() 时 config_cls 已持默认实例（字段默认值可用）。"""
        plugin = _make_plugin()
        assert plugin.config.arg_max_chars == 2000
        assert plugin.config.result_max_chars == 2000

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
        assert span["arg_truncated"] is False
        assert "a" * 3000 in span["arg_preview"]
        assert span["result_truncated"] is False
        assert "x" * 3000 in span["result_preview"]

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
        assert span["arg_truncated"] is True
        assert len(span["arg_preview"]) == 10 + len("...")
