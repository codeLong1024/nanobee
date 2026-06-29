"""
Phase 3 验收测试更新版 — SkillStage + 内置技能验证

覆盖验收用例：
1. SkillStage 从 skills/ 目录读取技能注入
2. 技能目录不存在时跳过，不报错
3. audit_logger 记录消息完成事件
4. _memory skill 始终注入
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin
from nanobee.kernel.context_pipeline import ContextPipeline, SkillStage
from nanobee.kernel.skill_manager import SkillsLoader
from nanobee.plugins.base import PluginMetadata


def _make_core_md(tmp_path: Path) -> Path:
    core_md = tmp_path / "core.md"
    core_md.write_text("# Test\n\n## Soul\n你是一个助手\n\n## Rules\n请遵守规则。\n", encoding="utf-8")
    return core_md


def _make_context_pipeline(tmp_path: Path,
                           builtin_skills_dir: Path | None = None) -> ContextPipeline:
    core_md = _make_core_md(tmp_path)
    return ContextPipeline(
        core_md_path=str(core_md),
        skill_loader=SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=builtin_skills_dir,
        ),
    )


def _make_skill_md(base_dir: Path, name: str, description: str, body: str) -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


class TestSkillStage:
    """验证内置 SkillStage 功能。"""

    @pytest.mark.asyncio
    async def test_injects_skills_into_prompt(self, tmp_path: Path):
        """SkillStage 渐进式注入：只注入元数据（name + description）。"""
        _make_skill_md(tmp_path / "skills", "git-log-analyzer",
                       "分析 git 提交历史",
                       "分析 git log 输出\n\n1. 获取提交列表\n2. 统计变更")

        stage = SkillStage(SkillsLoader(tmp_path / "skills"))
        context = {"system_prompt": "## Soul\n你是一个助手\n"}
        result = await stage.process(context)

        prompt = result["system_prompt"]
        assert "## 技能" in prompt
        assert "git-log-analyzer" in prompt
        assert "分析 git 提交历史" in prompt
        assert "**文件**" in prompt
        # 渐进式：不注入完整 body
        assert "1. 获取提交列表" not in prompt
        assert "2. 统计变更" not in prompt

    @pytest.mark.asyncio
    async def test_no_skills_no_injection(self, tmp_path: Path):
        """无技能时不注入技能段。"""
        stage = SkillStage(SkillsLoader(tmp_path / "skills"))
        context = {"system_prompt": "## Soul\n你是一个助手\n"}
        result = await stage.process(context)

        assert "## 技能" not in result["system_prompt"]

    @pytest.mark.asyncio
    async def test_memory_skill_always_injected(self, tmp_path: Path):
        """声明 full_inject 的技能始终全量注入 body。"""
        skill_dir = tmp_path / "skills" / "_memory"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: _memory\n"
            "description: 记忆管理\n"
            "full_inject: true\n"
            "---\n"
            "\n"
            "## 存储\n写入 memory/facts.md\n",
            encoding="utf-8",
        )

        stage = SkillStage(SkillsLoader(tmp_path / "skills"))
        context = {"system_prompt": "## Soul\n"}
        result = await stage.process(context)

        prompt = result["system_prompt"]
        assert "## 技能" in prompt
        assert "_memory" in prompt
        # full_inject 标记的技能 body 全量注入
        assert "## 存储" in prompt
        assert "memory/facts.md" in prompt

    @pytest.mark.asyncio
    async def test_builtin_skills_injected(self, tmp_path: Path):
        """内置技能也会被注入。"""
        _make_skill_md(tmp_path / "builtin", "_memory", "内置记忆", "内置记忆策略")
        _make_skill_md(tmp_path / "builtin", "tool-helper", "工具助手", "工具使用指南")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )
        stage = SkillStage(loader)
        context = {"system_prompt": "## Soul\n"}
        result = await stage.process(context)

        prompt = result["system_prompt"]
        assert "[builtin]" in prompt
        assert "内置记忆" in prompt or "工具助手" in prompt

    @pytest.mark.asyncio
    async def test_no_user_context_skips(self, tmp_path: Path):
        """设置不需要 user_context 也能工作。"""
        stage = SkillStage(SkillsLoader(tmp_path / "skills"))
        context = {"system_prompt": "## Soul\n你是一个助手\n"}
        result = await stage.process(context)

        # SkillStage 不再依赖 user_context
        assert result["system_prompt"] == "## Soul\n你是一个助手\n"


class TestAuditLoggerPlugin:
    """验证 audit_logger 插件功能。"""

    @pytest.mark.asyncio
    async def test_call_count_increments(self):
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
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))
        ctx = MagicMock()
        ctx.user_id = "test-user"

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
        ]
        await plugin.on_message_completed(ctx, messages)
        assert plugin.call_count == 1


class TestAllPluginsTogether:
    """验证插件同时启用的行为。"""

    @pytest.mark.asyncio
    async def test_audit_logger_does_not_affect_prompt(self, tmp_path: Path):
        """audit_logger 不贡献提示词内容。"""
        ctx = MagicMock()
        plugin = AuditLoggerPlugin(PluginMetadata(name="audit_logger", plugin_type="audit"))

        pipeline = _make_context_pipeline(tmp_path)

        result_with = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"},
            ctx,
            [plugin],
        )
        result_without = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"},
            ctx,
            [],
        )
        assert result_with == result_without
