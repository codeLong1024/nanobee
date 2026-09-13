"""上下文隔离与灵魂保护测试"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from nanobee.agent.messages import InboundMessage
from nanobee.exceptions import ContextError
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.soul_guard import SoulGuard, SoulViolationError
from nanobee.kernel.core_parser import CoreMDParser
from nanobee.utils.user_id import is_safe_user_id, resolve_storage_key


class _MockKernel:
    """ContextManager 所需的最小 kernel 桩。"""

    def __init__(self, data_dir):
        self.config = {"data_dir": str(data_dir)}
        self.data_dir = data_dir
        self.event_bus = None


@pytest.mark.asyncio
async def test_user_id_whitelist_accepts_realistic_ids(tmp_path):
    """白名单应放行现实形态的 user_id（uuid / 钉钉 cid / CLI 固定名）。"""
    manager = ContextManager(_MockKernel(tmp_path))
    for user_id in ("user-a", str(uuid.uuid4()), "a.b_c-d", "A" * 64):
        ctx = await manager.get_or_create(user_id)
        assert ctx.user_id == user_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    [
        "",                       # 空串
        "..",                     # 相对路径语义
        ".",                      # 当前目录语义
        "../escape",              # 路径遍历
        "a/b",                    # 路径分隔符
        "a\\b",                   # Windows 分隔符
        "has space",              # 空格
        "中文用户",                # 非白名单字符
        "x" * 65,                 # 超长（上界 64）
    ],
)
async def test_user_id_whitelist_rejects_and_creates_no_dir(tmp_path, bad_id):
    """非法 user_id 必须被拒绝，且不得创建任何越界目录。"""
    manager = ContextManager(_MockKernel(tmp_path))
    with pytest.raises(ContextError):
        await manager.get_or_create(bad_id)
    # 除 users 根目录外不得产生任何新目录（拒绝式校验，fail-visible）
    assert list(tmp_path.rglob("*")) == [tmp_path / "users"]


@pytest.mark.asyncio
async def test_user_id_validation_via_switch(tmp_path):
    """switch 委托 get_or_create，同样受白名单约束。"""
    manager = ContextManager(_MockKernel(tmp_path))
    with pytest.raises(ContextError):
        await manager.switch("../escape")


# ============================================================
# user_id 存储键归一化（评审 #1/#4 修复回归）
# ============================================================


class TestResolveStorageKey:
    """出生点归一：合法原样 / 非法哈希降级 / 相对路径拒绝。"""

    def test_safe_id_passthrough(self):
        """合法 id 原样返回——既有用户目录零迁移。"""
        for uid in ("user-a", "shenqla", "a.b_c-d", "A" * 64):
            assert resolve_storage_key(uid) == uid

    def test_unsafe_id_hashes_deterministically(self):
        """钉钉加密形态确定性映射为 u-<sha256[:32]>，同 raw 恒同 key。"""
        raw = "$:LWCP_v1:$6WU3eL8Xvn2qW40hDSakqZ7ESytOrAB5"
        key = resolve_storage_key(raw)
        expected = "u-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        assert key == expected
        assert resolve_storage_key(raw) == key

    def test_channel_prefix_hashed_and_safe(self):
        """含 ':' 的通道前缀 id（如 "dingtalk:cid..."）落白名单内存储键。"""
        key = resolve_storage_key("dingtalk:cidABC123")
        assert key.startswith("u-")
        assert len(key) == 34  # "u-" + 32 hex
        assert is_safe_user_id(key)

    def test_relative_path_semantics_rejected(self):
        """``.`` / ``..`` 是错误输入，fail-visible 拒绝而非净化。"""
        with pytest.raises(ContextError):
            resolve_storage_key(".")
        with pytest.raises(ContextError):
            resolve_storage_key("..")

    def test_non_string_rejected(self):
        with pytest.raises(ContextError):
            resolve_storage_key(None)  # type: ignore[arg-type]

    def test_empty_passthrough(self):
        """空串透传，由 InboundMessage.context_id 兜底分支接管后再归一。"""
        assert resolve_storage_key("") == ""


class TestInboundMessageContextIdNormalization:
    """隔离键出生点归一：InboundMessage.context_id 三个分支全覆盖。"""

    def test_dingtalk_encrypted_sender_hashed(self):
        msg = InboundMessage(
            channel="dingtalk", sender_id="$:LWCP_v1:$abc", chat_id="c", content="hi",
        )
        assert msg.context_id.startswith("u-")
        assert is_safe_user_id(msg.context_id)

    def test_safe_sender_passthrough(self):
        msg = InboundMessage(
            channel="dingtalk", sender_id="shenqla", chat_id="c", content="hi",
        )
        assert msg.context_id == "shenqla"

    def test_empty_sender_fallback_normalized(self):
        """空 sender_id 兜底 "channel:chat_id" 同样归一（CLI 场景）。"""
        msg = InboundMessage(
            channel="direct", sender_id="", chat_id="default", content="hi",
        )
        expected = "u-" + hashlib.sha256(b"direct:default").hexdigest()[:32]
        assert msg.context_id == expected

    def test_override_normalized(self):
        msg = InboundMessage(
            channel="direct", sender_id="u1", chat_id="c", content="hi",
            context_id_override="a/b",
        )
        assert msg.context_id.startswith("u-")
        assert is_safe_user_id(msg.context_id)


class TestSessionStoreSinkAssertion:
    """落点断言（评审 #1 回归）：越界 user_id 不得在 users/ 外写盘。"""

    def test_save_rejects_traversal_user_id(self, tmp_path):
        from nanobee.session.session_store import SessionStore

        store = SessionStore(tmp_path / "users")
        session = _make_session(user_id="../../escape", session_id="default")
        with pytest.raises(ContextError):
            store.save(session)
        # users/ 之外不得产生任何目录或文件
        assert list(tmp_path.iterdir()) == [tmp_path / "users"]

    def test_flush_all_never_writes_outside_users(self, tmp_path):
        """评审 #1 攻击链闭环验证：恶意 id 在拼路径时即被拒绝。

        落点断言位于 ``_session_path``（load/save 共用），故
        ``get_or_create → store.load`` 读盘前即拒绝——恶意 id 连
        SessionManager 缓存都进不去，关停 ``flush_all()`` 无从越界写。
        """
        from nanobee.session.session_manager import SessionManager

        manager = SessionManager(tmp_path / "users")
        with pytest.raises(ContextError):
            manager.get_or_create("../../escape", "default")
        assert list(tmp_path.iterdir()) == [tmp_path / "users"]


def _make_session(user_id: str, session_id: str):
    from nanobee.session.session import Session

    return Session(session_id=session_id, user_id=user_id)


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
