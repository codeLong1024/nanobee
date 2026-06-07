"""
UserContext 单元测试
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.user_context import UserContext, UserMetadata


class _FakeKernel:
    """测试用的假 kernel"""

    def __init__(self, work_dir: str) -> None:
        self.config = {"work_dir": work_dir}
        self.work_dir = Path(work_dir)


# ====== UserMetadata 测试 ======


def test_metadata_defaults():
    """默认元数据正确"""
    meta = UserMetadata({})
    assert meta.user_id == ""
    assert meta.display_name == ""
    assert meta.whitelist == []
    assert meta.blacklist == []


def test_metadata_from_dict():
    """从字典加载元数据"""
    meta = UserMetadata({
        "user_id": "user-alice",
        "display_name": "Alice",
        "whitelist": ["tool-a", "tool-b"],
        "blacklist": ["tool-bad"],
    })
    assert meta.user_id == "user-alice"
    assert meta.display_name == "Alice"
    assert meta.whitelist == ["tool-a", "tool-b"]
    assert meta.blacklist == ["tool-bad"]


def test_metadata_to_dict():
    """序列化为字典"""
    meta = UserMetadata({"user_id": "alice", "whitelist": ["echo"]})
    d = meta.to_dict()
    assert d["user_id"] == "alice"
    assert d["whitelist"] == ["echo"]


# ====== UserContext 测试 ======


def test_create_new_user(tmp_path: Path):
    """创建新用户时自动生成 context.yaml"""
    base_dir = tmp_path / "contexts" / "user-alice"
    ctx = UserContext("user-alice", base_dir)
    ctx._ensure_meta_file()

    assert ctx.user_id == "user-alice"
    assert ctx.base_dir == base_dir.resolve()
    assert ctx._metadata is None  # 懒加载，未访问

    # context.yaml 已创建
    assert ctx.meta_file.exists()
    with open(ctx.meta_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["user_id"] == "user-alice"
    assert data["whitelist"] == []
    assert data["blacklist"] == []


def test_metadata_lazy_load(tmp_path: Path):
    """元数据是懒加载的"""
    base_dir = tmp_path / "contexts" / "user-bob"
    ctx = UserContext("user-bob", base_dir)
    ctx._ensure_meta_file()

    # 访问前 _metadata 为 None
    assert ctx._metadata is None

    # 访问 metadata 属性触发加载
    meta = ctx.metadata
    assert meta.user_id == "user-bob"
    assert ctx._metadata is not None


def test_load_existing_user(tmp_path: Path):
    """加载已有用户上下文"""
    base_dir = tmp_path / "contexts" / "user-alice"
    ctx1 = UserContext("user-alice", base_dir)
    ctx1._ensure_meta_file()
    ctx1.add_message("user", "你好")
    ctx1.add_message("assistant", "你好！我是助手")

    # 重新加载
    ctx2 = UserContext("user-alice", base_dir)
    msgs = ctx2.get_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_metadata_only_no_history(tmp_path: Path):
    """get_metadata 不触发历史加载"""
    base_dir = tmp_path / "contexts" / "user-alice"
    ctx = UserContext("user-alice", base_dir)
    ctx._ensure_meta_file()

    # 添加一条历史
    ctx.add_message("user", "test")

    # 获取元数据（模拟 ContextManager.get_metadata）
    meta = ctx.metadata
    assert meta.user_id == "user-alice"
    assert ctx._conversation is not None


@pytest.mark.asyncio
async def test_context_manager_create(tmp_path: Path):
    """ContextManager 创建用户上下文"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    ctx = await cm.get_or_create("user-alice")
    assert ctx.user_id == "user-alice"
    assert ctx.meta_file.exists()


@pytest.mark.asyncio
async def test_context_manager_get_metadata(tmp_path: Path):
    """get_metadata 不加载历史"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    meta = await cm.get_metadata("user-bob")
    assert "user_id" in meta
    assert meta["user_id"] == "user-bob"

    # 添加消息
    ctx = await cm.get_or_create("user-bob")
    ctx.add_message("user", "test")

    # 再次获取元数据 - 不加载历史
    meta2 = await cm.get_metadata("user-bob")
    assert meta2["user_id"] == "user-bob"


@pytest.mark.asyncio
async def test_context_manager_switch(tmp_path: Path):
    """切换用户上下文"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    ctx_a = await cm.switch("user-a")
    ctx_b = await cm.switch("user-b")

    assert ctx_a.user_id == "user-a"
    assert ctx_b.user_id == "user-b"
    assert ctx_a is not ctx_b

    # 同一用户返回同一实例
    ctx_a2 = await cm.switch("user-a")
    assert ctx_a2 is ctx_a


@pytest.mark.asyncio
async def test_context_manager_list(tmp_path: Path):
    """列出所有用户上下文"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    await cm.get_or_create("user-a")
    await cm.get_or_create("user-b")

    assert set(cm.list_contexts()) == {"user-a", "user-b"}


# ====== tmp 目录测试 ======


def test_conversation_context_creates_tmp_dir(tmp_path: Path):
    """ConversationContext 创建 tmp/ 目录"""
    from nanobee.kernel.user_context import ConversationContext
    ctx = ConversationContext("test", tmp_path)
    assert ctx.tmp_dir == tmp_path / "tmp"
    assert ctx.tmp_dir.exists()
    assert ctx.tmp_dir.is_dir()


def test_user_context_exposes_tmp_dir(tmp_path: Path):
    """UserContext 暴露 tmp_dir 属性"""
    user_ctx = UserContext("test-user", tmp_path)
    assert user_ctx.tmp_dir == tmp_path / "tmp"
    assert user_ctx.tmp_dir.exists()


def test_plugin_tmp_returns_none_without_context_var():
    """没有绑定 ContextVar 时 plugin.tmp 返回 None"""
    from nanobee.plugins.base import NanobeePlugin
    plugin = NanobeePlugin()
    assert plugin.tmp is None


@pytest.mark.asyncio
async def test_plugin_tmp_with_context_var(tmp_path: Path):
    """绑定 ContextVar 后 plugin.tmp 返回 per-plugin 路径"""
    from nanobee.kernel.context_sandbox_var import bind_tmp, reset_tmp
    from nanobee.plugins.base import NanobeePlugin

    plugin = NanobeePlugin()
    token = bind_tmp(tmp_path)
    try:
        result = plugin.tmp
        assert result is not None
        # 应返回 tmp/<plugin_name>/
        assert result == tmp_path / "base"
        assert result.exists()
    finally:
        reset_tmp(token)
