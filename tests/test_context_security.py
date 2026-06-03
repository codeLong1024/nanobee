"""上下文隔离与灵魂保护测试"""

from __future__ import annotations

import os
import pytest

from nanobee.kernel.context_manager import ContextManager, ConversationContext
from nanobee.kernel.soul_guard import SoulGuard, SoulViolationError
from nanobee.kernel.core_parser import CoreMDParser


@pytest.mark.asyncio
async def test_context_isolation(tmp_path):
    """测试上下文隔离"""
    # 模拟 kernel
    class MockKernel:
        def __init__(self):
            self.config = {"work_dir": str(tmp_path)}
            self.event_bus = None

    kernel = MockKernel()
    manager = ContextManager(kernel)

    # 创建两个上下文
    ctx1 = await manager.get_or_create("user-a")
    ctx2 = await manager.get_or_create("user-b")

    # 添加消息
    ctx1.add_message("user", "Hello from A")
    ctx2.add_message("user", "Hello from B")

    # 验证隔离
    messages1 = ctx1.get_messages()
    messages2 = ctx2.get_messages()

    assert len(messages1) == 1
    assert len(messages2) == 1
    assert messages1[0]["content"] == "Hello from A"
    assert messages2[0]["content"] == "Hello from B"

    # 验证目录隔离
    assert ctx1.base_dir != ctx2.base_dir
    assert (ctx1.base_dir / "history.jsonl").exists()
    assert (ctx2.base_dir / "history.jsonl").exists()


@pytest.mark.asyncio
async def test_soul_guard_hash_check(tmp_path):
    """测试灵魂守卫哈希校验"""
    core_md = tmp_path / "core.md"
    core_md.write_text("# Test\n\n## Soul\nTest personality\n", encoding="utf-8")

    class MockKernel:
        def __init__(self):
            self.config = {"core_md_path": str(core_md)}
            self.event_bus = None

    kernel = MockKernel()
    guard = SoulGuard(kernel)

    # 第一次检查应通过
    await guard.check()

    # 恢复写入权限并篡改文件
    os.chmod(core_md, 0o644)
    core_md.write_text("# Tampered\n\n## Soul\nHacked!\n", encoding="utf-8")

    # 第二次检查应失败
    with pytest.raises(SoulViolationError):
        await guard.check()


@pytest.mark.asyncio
async def test_soul_guard_intercept_write(tmp_path):
    """测试写入拦截"""
    core_md = tmp_path / "core.md"
    core_md.write_text("# Test\n", encoding="utf-8")

    from nanobee.kernel.event_bus import EventBus

    class MockKernel:
        def __init__(self):
            self.config = {"core_md_path": str(core_md)}
            self.event_bus = EventBus()

    kernel = MockKernel()
    guard = SoulGuard(kernel)

    # 尝试写入 core.md 应被拦截
    assert not await guard.intercept_write(core_md, "hacked")

    # 尝试写入其他文件应被允许
    other_file = tmp_path / "other.txt"
    assert await guard.intercept_write(other_file, "safe content")


@pytest.mark.asyncio
async def test_soul_guard_auto_create_core_md(tmp_path):
    """测试灵魂守卫自动创建默认 core.md"""
    class MockKernel:
        def __init__(self):
            self.config = {"core_md_path": str(tmp_path / "core.md")}
            self.event_bus = None

    kernel = MockKernel()
    guard = SoulGuard(kernel)

    # core.md 不存在时应自动创建
    assert not (tmp_path / "core.md").exists()
    await guard.check()
    assert (tmp_path / "core.md").exists()

    # 验证文件内容包含 Soul 和 Rules 段
    parser = CoreMDParser(tmp_path / "core.md")
    sections = parser.parse()
    # section 名称可能是 "Soul（人格）" 或 "Soul"，检查包含 Soul 的键
    assert any("Soul" in k for k in sections)
    assert any("Rules" in k for k in sections)
