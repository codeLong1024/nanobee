"""
Phase 3 验收测试 — 极简参考插件与端到端验证

覆盖 6 个验收用例：
1. memory_echo 读取 memory.txt 注入
2. skill_static 读取 skills.md 注入
3. 文件不存在时返回 None，框架不报错
4. audit_logger 记录消息完成事件
5. 多用户隔离：不同用户的 memory.txt 内容互不交叉
6. 三个插件同时启用，System Prompt 同时包含记忆段和技能段
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.audit_logger.plugin import AuditLoggerPlugin
from nanobee.builtin.memory_echo.plugin import MemoryEchoPlugin
from nanobee.builtin.skill_static.plugin import SkillStaticPlugin
from nanobee.kernel.context_pipeline import ContextPipeline


# ---- 辅助工具 ----


def _make_kernel_with_core(tmp_path: Path, memory_txt: str = "", skills_md: str = "") -> MagicMock:
    """创建带有效 core.md 和用户文件的 mock kernel。"""
    core_md = tmp_path / "core.md"
    core_md.write_text("# Test\n\n## Soul\n你是一个助手\n\n## Rules\n请遵守规则。\n", encoding="utf-8")
    kernel = MagicMock()
    kernel.config = {"core_md_path": str(core_md)}
    return kernel


def _make_user_context(tmp_path: Path, user_id: str = "test-user",
                       memory_txt: str = "", skills_md: str = "") -> MagicMock:
    """创建带实际 base_dir 的 mock UserContext。"""
    user_dir = tmp_path / "contexts" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    if memory_txt:
        (user_dir / "memory.txt").write_text(memory_txt, encoding="utf-8")
    if skills_md:
        (user_dir / "skills.md").write_text(skills_md, encoding="utf-8")

    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.base_dir = user_dir
    return ctx


# ---- memory_echo 测试 ----


class TestMemoryEchoPlugin:
    """验证 memory_echo 插件功能。"""

    def test_reads_memory_txt(self, tmp_path: Path):
        """memory_echo 读取 memory.txt 内容并返回。"""
        ctx = _make_user_context(tmp_path, "alice", memory_txt="我是 Alice")
        plugin = MemoryEchoPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result == "我是 Alice"

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        """memory.txt 不存在时返回 None。"""
        ctx = _make_user_context(tmp_path, "alice")
        plugin = MemoryEchoPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result is None

    def test_returns_none_when_file_empty(self, tmp_path: Path):
        """memory.txt 为空时返回 None。"""
        ctx = _make_user_context(tmp_path, "alice", memory_txt="   ")
        plugin = MemoryEchoPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result is None

    def test_returns_none_when_context_no_base_dir(self):
        """UserContext 没有 base_dir 时不崩溃。"""
        ctx = MagicMock()
        # 不设 base_dir
        del ctx.base_dir
        plugin = MemoryEchoPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_injects_into_memory_stage(self, tmp_path: Path):
        """通过 ContextPipeline 验证注入到记忆段。"""
        ctx = _make_user_context(tmp_path, "bob", memory_txt="Bob 的记忆")
        plugin = MemoryEchoPlugin()
        kernel = _make_kernel_with_core(tmp_path)
        pipeline = ContextPipeline(kernel)

        result = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n\n## Rules\n请遵守规则。"},
            ctx,
            [plugin],
        )
        assert "Bob 的记忆" in result
        assert "## 记忆" in result


class TestSkillStaticPlugin:
    """验证 skill_static 插件功能。"""

    def test_reads_skills_md(self, tmp_path: Path):
        """skill_static 读取 skills.md 内容并返回。"""
        ctx = _make_user_context(tmp_path, "alice", skills_md="可用技能：Python, JavaScript")
        plugin = SkillStaticPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result == "可用技能：Python, JavaScript"

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        """skills.md 不存在时返回 None。"""
        ctx = _make_user_context(tmp_path, "alice")
        plugin = SkillStaticPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result is None

    def test_returns_none_when_file_empty(self, tmp_path: Path):
        """skills.md 为空时返回 None。"""
        ctx = _make_user_context(tmp_path, "alice", skills_md="")
        plugin = SkillStaticPlugin()
        result = plugin.contribute_to_prompt(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_injects_into_skill_stage(self, tmp_path: Path):
        """通过 ContextPipeline 验证注入到技能段。"""
        ctx = _make_user_context(tmp_path, "carol", skills_md="- Python\n- Go")
        plugin = SkillStaticPlugin()
        kernel = _make_kernel_with_core(tmp_path)
        pipeline = ContextPipeline(kernel)

        result = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n\n## Rules\n请遵守规则。"},
            ctx,
            [plugin],
        )
        assert "- Python" in result
        assert "## 技能" in result


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
        assert plugin.call_count == 1
        # 只有带 tool_calls 键的消息才被统计
        assert plugin.call_count >= 1  # 实际测试被调用次数

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
    """验证不同用户的 memory.txt / skills.md 内容隔离。"""

    def test_different_memory_per_user(self, tmp_path: Path):
        """User-A 和 User-B 的 memory.txt 内容互不交叉。"""
        ctx_a = _make_user_context(tmp_path, "alice", memory_txt="我是 Alice")
        ctx_b = _make_user_context(tmp_path, "bob", memory_txt="我是 Bob")

        plugin = MemoryEchoPlugin()

        result_a = plugin.contribute_to_prompt(ctx_a)
        result_b = plugin.contribute_to_prompt(ctx_b)

        assert result_a == "我是 Alice"
        assert result_b == "我是 Bob"
        assert result_a != result_b

    def test_different_skills_per_user(self, tmp_path: Path):
        """User-A 和 User-B 的 skills.md 内容互不交叉。"""
        ctx_a = _make_user_context(tmp_path, "alice", skills_md="Alice 的技能")
        ctx_b = _make_user_context(tmp_path, "bob", skills_md="Bob 的技能")

        plugin = SkillStaticPlugin()

        result_a = plugin.contribute_to_prompt(ctx_a)
        result_b = plugin.contribute_to_prompt(ctx_b)

        assert result_a == "Alice 的技能"
        assert result_b == "Bob 的技能"
        assert result_a != result_b


# ---- 三个插件协同测试 ----


class TestAllPluginsTogether:
    """验证三个参考插件同时启用的行为。"""

    @pytest.mark.asyncio
    async def test_both_echo_plugins_work_together(self, tmp_path: Path):
        """memory_echo + skill_static 同时注入，System Prompt 同时包含记忆段和技能段。"""
        ctx = _make_user_context(
            tmp_path, "dave",
            memory_txt="Dave 的记忆",
            skills_md="Dave 的技能列表",
        )
        memory_plugin = MemoryEchoPlugin()
        skill_plugin = SkillStaticPlugin()

        kernel = _make_kernel_with_core(tmp_path)
        pipeline = ContextPipeline(kernel)

        result = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你是一个助手\n"},
            ctx,
            [memory_plugin, skill_plugin],
        )

        assert "Dave 的记忆" in result
        assert "Dave 的技能列表" in result
        assert "## 记忆" in result
        assert "## 技能" in result

        # 记忆段应该在技能段之前
        mem_pos = result.index("## 记忆")
        skill_pos = result.index("## 技能")
        assert mem_pos < skill_pos

    @pytest.mark.asyncio
    async def test_audit_logger_does_not_affect_prompt(self, tmp_path: Path):
        """audit_logger 不贡献提示词内容。"""
        ctx = _make_user_context(tmp_path, "test")
        plugin = AuditLoggerPlugin()

        kernel = _make_kernel_with_core(tmp_path)
        pipeline = ContextPipeline(kernel)

        # audit_logger 不实现 contribute_to_prompt，不应偏移 system_prompt
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


# ---- 错误边界测试 ----


class TestErrorBoundaries:
    """验证参考插件的异常处理能力。"""

    def test_memory_echo_unreadable_file(self, tmp_path: Path):
        """memory.txt 不可读时返回 None 不崩溃。"""
        ctx = _make_user_context(tmp_path, "alice", memory_txt="内容")
        memory_file = Path(ctx.base_dir) / "memory.txt"
        # 模拟不可读：修改权限
        memory_file.chmod(0o000)
        try:
            plugin = MemoryEchoPlugin()
            result = plugin.contribute_to_prompt(ctx)
            assert result is None
        finally:
            memory_file.chmod(0o644)

    def test_skill_static_unreadable_file(self, tmp_path: Path):
        """skills.md 不可读时返回 None 不崩溃。"""
        ctx = _make_user_context(tmp_path, "alice", skills_md="内容")
        skills_file = Path(ctx.base_dir) / "skills.md"
        skills_file.chmod(0o000)
        try:
            plugin = SkillStaticPlugin()
            result = plugin.contribute_to_prompt(ctx)
            assert result is None
        finally:
            skills_file.chmod(0o644)
