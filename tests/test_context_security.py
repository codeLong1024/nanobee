"""上下文隔离与灵魂保护测试"""

from __future__ import annotations

import os
import pytest

from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.user_context import UserContext
from nanobee.kernel.soul_guard import SoulGuard, SoulViolationError
from nanobee.kernel.core_parser import CoreMDParser


@pytest.mark.asyncio
async def test_context_isolation(tmp_path):
    """测试上下文隔离（基于 SessionManager）"""
    from nanobee.session.session_manager import SessionManager

    # 模拟 kernel
    class MockKernel:
        def __init__(self):
            self.config = {"data_dir": str(tmp_path)}
            self.data_dir = tmp_path
            self.event_bus = None

    kernel = MockKernel()
    manager = ContextManager(kernel)
    session_manager = SessionManager(tmp_path / "users")

    # 创建两个上下文
    await manager.get_or_create("user-a")
    await manager.get_or_create("user-b")

    # 通过 SessionManager 添加消息（多 session 隔离）
    s_a = session_manager.get_or_create("user-a", "dingtalk:chat-a")
    s_a.add_message("user", "Hello from A")
    session_manager.save(s_a)

    s_b = session_manager.get_or_create("user-b", "dingtalk:chat-b")
    s_b.add_message("user", "Hello from B")
    session_manager.save(s_b)

    # 验证隔离
    loaded_a = session_manager.get_or_create("user-a", "dingtalk:chat-a")
    loaded_b = session_manager.get_or_create("user-b", "dingtalk:chat-b")

    assert len(loaded_a.messages) == 1
    assert len(loaded_b.messages) == 1
    assert loaded_a.messages[0]["content"] == "Hello from A"
    assert loaded_b.messages[0]["content"] == "Hello from B"

    # 验证 sessions 目录隔离
    assert (tmp_path / "users" / "user-a" / "sessions" / "dingtalk_chat-a.jsonl").exists()
    assert (tmp_path / "users" / "user-b" / "sessions" / "dingtalk_chat-b.jsonl").exists()


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

    from nanobee.events.event_bus import EventBus
    from nanobee.events.runtime_events import RuntimeEventBus

    class MockKernel:
        def __init__(self):
            self.config = {"core_md_path": str(core_md)}
            self.event_bus = EventBus()
            self.runtime_events = RuntimeEventBus()

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
    assert any("Soul" in k for k in sections)
    assert any("Rules" in k for k in sections)
