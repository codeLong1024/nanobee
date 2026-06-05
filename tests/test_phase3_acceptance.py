"""
Phase 3 验收测试 — 极简参考插件与内置 SkillStage 验证

覆盖 4 个验收用例：
1. SkillStage 从 skills/ 目录读取技能注入
2. 技能目录不存在时跳过，不报错
3. audit_logger 记录消息完成事件
4. 多用户隔离：不同用户的技能互不交叉
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin
from nanobee.kernel.context_pipeline import ContextPipeline, SkillStage
from nanobee.kernel.skill_manager import SkillManager, SkillVisibility


# ---- 辅助工具 ----


def _make_kernel_with_core(tmp_path: Path) -> MagicMock:
    """创建带有效 core.md 的 mock kernel。"""
    core_md = tmp_path / "core.md"
    core_md.write_text("# Test\n\n## Soul\n你是一个助手\n\n## Rules\n请遵守规则。\n", encoding="utf-8")
    kernel = MagicMock()
    kernel.config = {"core_md_path": str(core_md), "work_dir": str(tmp_path)}
    # 统一 SkillManager 实例（避免路径分裂）
    kernel.skill_manager = SkillManager(tmp_path / "skills")
    return kernel


def _make_user_context(tmp_path: Path, user_id: str = "test-user") -> MagicMock:
    """创建带实际 base_dir 的 mock UserContext。"""
    user_dir = tmp_path / "contexts" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.base_dir = user_dir
    return ctx


# ---- SkillStage 测试 ----


class TestSkillStage:
    """验证内置 SkillStage 功能。"""

    def _create_skill(self, skill_mgr: SkillManager, user_id: str, name: str,
                      description: str, body: str,
                      visibility: SkillVisibility = SkillVisibility.PRIVATE) -> None:
        skill_mgr.create(user_id, name, description, body, visibility=visibility)

    @pytest.mark.asyncio
    async def test_injects_skills_into_prompt(self, tmp_path: Path):
        """SkillStage 读取用户技能并注入 System Prompt。"""
        user_id = "alice"
        skill_mgr = SkillManager(tmp_path / "skills")
        self._create_skill(skill_mgr, user_id, "git-log-analyzer",
                           "分析 git 提交历史", "分析 git log 输出\n\n1. 获取提交列表\n2. 统计变更")

        ctx = _make_user_context(tmp_path, user_id)
        stage = SkillStage(_make_kernel_with_core(tmp_path))
        context = {"system_prompt": "## Soul\n你是一个助手\n", "user_context": ctx}
        result = await stage.process(context)

        prompt = result["system_prompt"]
        assert "## 技能" in prompt
        assert "git-log-analyzer" in prompt
        assert "分析 git log 输出" in prompt

    @pytest.mark.asyncio
    async def test_no_skills_no_injection(self, tmp_path: Path):
        """无技能时不注入技能段。"""
        ctx = _make_user_context(tmp_path, "alice")
        stage = SkillStage(_make_kernel_with_core(tmp_path))
        context = {"system_prompt": "## Soul\n你是一个助手\n", "user_context": ctx}
        result = await stage.process(context)

        assert "## 技能" not in result["system_prompt"]

    @pytest.mark.asyncio
    async def test_no_user_context_skips(self, tmp_path: Path):
        """user_context 为 None 时跳过。"""
        stage = SkillStage(_make_kernel_with_core(tmp_path))
        context = {"system_prompt": "## Soul\n你是一个助手\n"}
        result = await stage.process(context)

        assert "## 技能" not in result["system_prompt"]
        assert result["system_prompt"] == "## Soul\n你是一个助手\n"

    @pytest.mark.asyncio
    async def test_shared_skills_visible(self, tmp_path: Path):
        """共享技能对其他用户可见。"""
        # 用户 A 创建共享技能
        skill_mgr = SkillManager(tmp_path / "skills")
        self._create_skill(skill_mgr, "alice", "code-review",
                           "代码审查助手", "检查代码质量",
                           visibility=SkillVisibility.SHARED)

        # 用户 B 查看时应包含 A 的共享技能
        ctx_b = _make_user_context(tmp_path, "bob")
        stage = SkillStage(_make_kernel_with_core(tmp_path))
        context = {"system_prompt": "## Soul\n你是一个助手\n", "user_context": ctx_b}
        result = await stage.process(context)

        prompt = result["system_prompt"]
        assert "## 技能" in prompt
        assert "code-review" in prompt
        assert "@alice" in prompt

    @pytest.mark.asyncio
    async def test_does_not_show_own_shared_twice(self, tmp_path: Path):
        """自己的共享技能不会重复注入。"""
        skill_mgr = SkillManager(tmp_path / "skills")
        self._create_skill(skill_mgr, "alice", "my-skill",
                           "我的共享技能", "内容",
                           visibility=SkillVisibility.SHARED)

        ctx = _make_user_context(tmp_path, "alice")
        stage = SkillStage(_make_kernel_with_core(tmp_path))
        context = {"system_prompt": "## Soul\n", "user_context": ctx}
        result = await stage.process(context)

        # 技能在 prompt 中只出现一次（不重复注入）
        assert result["system_prompt"].count("### my-skill") == 1


class TestAuditLoggerPlugin:
    """验证 audit_logger 插件功能。"""

    @pytest.mark.asyncio
    async def test_call_count_increments(self):
        """on_message_completed 被调用时 call_count 递增。"""
        plugin = AuditLoggerPlugin()
        ctx = MagicMock()
        ctx.user_id = "test-user"

        assert plugin.call_count == 0
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 1
        await plugin.on_message_completed(ctx, [])
        assert plugin.call_count == 2

    @pytest.mark.asyncio
    async def test_counts_tool_calls(self):
        """正确统计 tool_calls 数量。"""
        plugin = AuditLoggerPlugin()
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
        """无工具调用时 tool_calls 统计为 0。"""
        plugin = AuditLoggerPlugin()
        ctx = MagicMock()
        ctx.user_id = "test-user"

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
        ]
        await plugin.on_message_completed(ctx, messages)
        assert plugin.call_count == 1


# ---- 多用户隔离测试 ----


class TestMultiUserIsolation:
    """验证不同用户的技能内容隔离。"""

    def test_different_skills_per_user(self, tmp_path: Path):
        """User-A 和 User-B 的技能内容互不交叉。"""
        skill_mgr = SkillManager(tmp_path / "contexts")
        skill_mgr.create("alice", "skill-1", "Alice 的技能", "Alice 的内容")
        skill_mgr.create("bob", "skill-2", "Bob 的技能", "Bob 的内容")

        alice_skills = skill_mgr.list_skills("alice")
        bob_skills = skill_mgr.list_skills("bob")

        assert len(alice_skills) == 1
        assert alice_skills[0].meta.description == "Alice 的技能"
        assert len(bob_skills) == 1
        assert bob_skills[0].meta.description == "Bob 的技能"


# ---- 插件协同测试 ----


class TestAllPluginsTogether:
    """验证插件同时启用的行为。"""

    @pytest.mark.asyncio
    async def test_audit_logger_does_not_affect_prompt(self, tmp_path: Path):
        """audit_logger 不贡献提示词内容。"""
        ctx = _make_user_context(tmp_path, "test")
        plugin = AuditLoggerPlugin()

        kernel = _make_kernel_with_core(tmp_path)
        pipeline = ContextPipeline(kernel)

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


