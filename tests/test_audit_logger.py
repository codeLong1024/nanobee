"""AuditLogger 插件测试 — 消息完成事件的审计计数。

覆盖场景：
1. call_count 计数递增
2. 带 tool_calls 的消息计数
3. 无 tool_calls 的消息计数
4. 不影响 prompt 内容
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin
from nanobee.kernel.context_pipeline import ContextPipeline
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


class TestAuditLoggerPlugin:
    """AuditLoggerPlugin 功能测试。"""

    @pytest.mark.asyncio
    async def test_call_count_increments(self):
        """call_count 每次 on_message_completed 递增。"""
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))
        ctx = MagicMock()
        ctx.user_id = "test-user"

        assert plugin.call_count == 0
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 1
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 2

    @pytest.mark.asyncio
    async def test_counts_tool_calls(self):
        """包含 tool_calls 的消息正常计数。"""
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))
        ctx = MagicMock()
        ctx.user_id = "test-user"

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "结果"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_2"}, {"id": "call_3"}]},
            {"role": "assistant", "content": "完成"},
        ]
        await plugin.on_message_completed(ctx, messages)
        assert plugin.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_tool_calls(self):
        """无 tool_calls 的消息正常计数。"""
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))
        ctx = MagicMock()
        ctx.user_id = "test-user"

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
        ]
        await plugin.on_message_completed(ctx, messages)
        assert plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_audit_logger_does_not_affect_prompt(self, tmp_path: Path):
        """audit_logger 不贡献提示词内容。"""
        ctx = MagicMock()
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))
        pipeline = _make_context_pipeline(tmp_path)

        result_with = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"}, ctx, [plugin],
        )
        result_without = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"}, ctx, [],
        )
        assert result_with == result_without
