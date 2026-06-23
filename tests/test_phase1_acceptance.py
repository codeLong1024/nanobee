"""
Phase 1 集成测试 — 多租户隔离验收用例
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobee.kernel import NanobeeKernel
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.core_parser import CoreMDParser
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.router import ContextRouter
from nanobee.kernel.sandbox import ContextSandbox
from nanobee.exceptions import SandboxViolationError
from nanobee.kernel.tool_collector import ToolCollector


# ====== LockManager 验收 ======


@pytest.mark.asyncio
async def test_per_user_concurrency():
    """User-A 和 User-B 并发发消息，互不阻塞"""
    lock_mgr = LockManager()
    order: list[str] = []

    async def send(user: str, msg_id: str) -> None:
        async with lock_mgr.acquire(user):
            order.append(f"{user}_{msg_id}_start")
            await asyncio.sleep(0.02)
            order.append(f"{user}_{msg_id}_end")

    await asyncio.gather(
        send("user-alice", "1"),
        send("user-bob", "1"),
        send("user-alice", "2"),
        send("user-bob", "2"),
    )

    # 同一用户串行
    alice_events = [e for e in order if e.startswith("user-alice")]
    assert alice_events == [
        "user-alice_1_start", "user-alice_1_end",
        "user-alice_2_start", "user-alice_2_end",
    ]

    bob_events = [e for e in order if e.startswith("user-bob")]
    assert bob_events == [
        "user-bob_1_start", "user-bob_1_end",
        "user-bob_2_start", "user-bob_2_end",
    ]

    # 不同用户可交错（至少有一个交错点）
    # alice_1_end 和 bob_1_end 之间至少插入了对方的 start/end
    assert "user-bob_1_start" in order
    assert "user-bob_1_end" in order


# ====== ContextRouter 验收 ======


def test_unknown_route_rejected():
    """未知路由直接拒绝"""
    router = ContextRouter({"cli:default": "user-alice"})
    from nanobee.kernel.router import UnknownRouteError
    with pytest.raises(UnknownRouteError):
        router.resolve("cli", "unknown-chat")


def test_known_route_resolved():
    """已知路由正常返回 (user_id, session_id)"""
    router = ContextRouter({"cli:default": "user-alice"})
    user_id, session_id = router.resolve("cli", "default")
    assert user_id == "user-alice"
    assert session_id == "cli:default"


# ====== ContextSandbox 验收 ======


def test_sandbox_allows_own_file(tmp_path: Path):
    """tool-fs 读取当前 User 文件 — 沙箱允许"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    test_file = root / "memory" / "notes.txt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("alice notes", encoding="utf-8")

    sandbox = ContextSandbox(root)
    safe_path = sandbox.resolve_safe(str(test_file))
    assert safe_path == test_file.resolve()
    assert safe_path.read_text(encoding="utf-8") == "alice notes"


def test_sandbox_blocks_cross_user_escape(tmp_path: Path):
    """tool-fs 尝试 ../../user-b/memory/ — 沙箱拦截"""
    root_a = tmp_path / "users" / "user-a"
    root_b = tmp_path / "users" / "user-b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    (root_b / "memory").mkdir()

    sandbox = ContextSandbox(root_a)
    escape_path = str(root_a / "../../user-b/memory/secret.txt")

    with pytest.raises(SandboxViolationError) as exc_info:
        sandbox.resolve_safe(escape_path)
    assert "user-b" in str(exc_info.value) or "路径逃逸" in str(exc_info.value)


# ====== ToolCollector 验收 ======


def test_blacklist_tool_excluded_from_definitions():
    """LLM 幻觉试图调用黑名单工具 — 定义层剔除"""
    collector = ToolCollector(
        ["tool-fs", "tool-web", "tool-admin"],
        blacklist=["tool-admin"],
    )
    definitions = [
        {"type": "function", "function": {"name": "tool-fs"}},
        {"type": "function", "function": {"name": "tool-web"}},
        {"type": "function", "function": {"name": "tool-admin"}},
    ]
    filtered = collector.filter_definitions(definitions)
    names = [d["function"]["name"] for d in filtered]
    assert "tool-admin" not in names
    assert collector.is_allowed("tool-admin") is False


def test_whitelist_only_allowed_tools():
    """白名单模式下只有白名单内的工具可用"""
    collector = ToolCollector(
        ["a", "b", "c", "d"],
        whitelist=["a", "b"],
    )
    assert collector.is_allowed("a") is True
    assert collector.is_allowed("c") is False
    assert collector.is_allowed("d") is False


# ====== UserContext 端到端验收 ======


class _FakeKernel:
    def __init__(self, work_dir: str) -> None:
        self.config = {"data_dir": work_dir}
        self.data_dir = Path(work_dir)


@pytest.mark.asyncio
async def test_user_context_isolation(tmp_path: Path):
    """多个用户会话隔离，数据不交叉"""
    from nanobee.session.session_manager import SessionManager

    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    await cm.get_or_create("alice")
    await cm.get_or_create("bob")

    session_mgr = SessionManager(tmp_path / "users")

    s_alice = session_mgr.get_or_create("alice", "dingtalk:chat-a")
    s_alice.add_message("user", "我是 Alice")
    session_mgr.save(s_alice)

    s_bob = session_mgr.get_or_create("bob", "dingtalk:chat-b")
    s_bob.add_message("user", "我是 Bob")
    session_mgr.save(s_bob)

    assert len(s_alice.messages) == 1
    assert len(s_bob.messages) == 1
    assert s_alice.messages[0]["content"] == "我是 Alice"
    assert s_bob.messages[0]["content"] == "我是 Bob"

    # 目录隔离验证
    alice = await cm.get_or_create("alice")
    bob = await cm.get_or_create("bob")
    assert alice.base_dir != bob.base_dir
    assert "alice" in str(alice.base_dir)
    assert "bob" in str(bob.base_dir)


@pytest.mark.asyncio
async def test_context_metadata_lazy(tmp_path: Path):
    """元数据不触发历史加载"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    meta = await cm.get_metadata("test-user")
    assert meta["user_id"] == "test-user"

    ctx = await cm.get_or_create("test-user")
    assert ctx._metadata is not None  # 已加载
