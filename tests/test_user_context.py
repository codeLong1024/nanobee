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
        self.config = {"data_dir": work_dir}
        self.data_dir = Path(work_dir)


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
    """创建新用户时自动生成 identity.yaml"""
    base_dir = tmp_path / "users" / "user-alice"
    ctx = UserContext("user-alice", base_dir)
    ctx._ensure_identity_file()

    assert ctx.user_id == "user-alice"
    assert ctx.base_dir == base_dir.resolve()
    assert ctx._metadata is None  # 懒加载，未访问

    # identity.yaml 已创建
    assert ctx.meta_file.exists()
    with open(ctx.meta_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["user_id"] == "user-alice"
    assert data["whitelist"] == []
    assert data["blacklist"] == []


def test_metadata_lazy_load(tmp_path: Path):
    """元数据是懒加载的"""
    base_dir = tmp_path / "users" / "user-bob"
    ctx = UserContext("user-bob", base_dir)
    ctx._ensure_identity_file()

    # 访问前 _metadata 为 None
    assert ctx._metadata is None

    # 访问 metadata 属性触发加载
    meta = ctx.metadata
    assert meta.user_id == "user-bob"
    assert ctx._metadata is not None


def test_user_directories_created(tmp_path: Path):
    """UserContext 创建时自动创建子目录"""
    base_dir = tmp_path / "users" / "user-alice"
    ctx = UserContext("user-alice", base_dir)

    # 子目录已创建
    assert ctx.work_dir.exists()
    assert ctx.memory_dir.exists()
    assert ctx.tmp_dir.exists()


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
    """get_metadata 加载元数据"""
    kernel = _FakeKernel(str(tmp_path))
    cm = ContextManager(kernel)

    meta = await cm.get_metadata("user-bob")
    assert "user_id" in meta
    assert meta["user_id"] == "user-bob"

    # 再次获取元数据
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


def test_user_context_creates_tmp_dir(tmp_path: Path):
    """UserContext 创建 .tmp/ 目录"""
    user_ctx = UserContext("test", tmp_path)
    assert user_ctx.tmp_dir == tmp_path / ".tmp"
    assert user_ctx.tmp_dir.exists()
    assert user_ctx.tmp_dir.is_dir()


def test_user_context_exposes_tmp_dir(tmp_path: Path):
    """UserContext 暴露 tmp_dir 属性"""
    user_ctx = UserContext("test-user", tmp_path)
    assert user_ctx.tmp_dir == tmp_path / ".tmp"
    assert user_ctx.tmp_dir.exists()


def test_plugin_tmp_returns_none_without_context_var():
    """没有绑定 ContextVar 时 plugin.tmp 返回 None"""
    from nanobee.plugins.base import NanobeePlugin, PluginMetadata
    plugin = NanobeePlugin(PluginMetadata(name="base", plugin_type="unknown"))
    assert plugin.tmp is None


@pytest.mark.asyncio
async def test_plugin_tmp_with_context_var(tmp_path: Path):
    """绑定 ContextVar 后 plugin.tmp 返回 per-plugin 路径"""
    from nanobee.kernel.context_sandbox_var import bind_tmp, reset_tmp
    from nanobee.plugins.base import NanobeePlugin, PluginMetadata

    plugin = NanobeePlugin(PluginMetadata(name="base", plugin_type="unknown"))
    token = bind_tmp(tmp_path)
    try:
        result = plugin.tmp
        assert result is not None
        assert result == tmp_path / "base"
        assert result.exists()
    finally:
        reset_tmp(token)
