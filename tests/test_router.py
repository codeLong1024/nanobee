"""
ContextRouter 单元测试
"""

from __future__ import annotations

import pytest

from nanobee.kernel.router import ContextRouter, UnknownRouteError


def _uid(res: tuple[str, str]) -> str:
    """Helper: extract user_id from resolve result."""
    return res[0]


def test_route_known_channel():
    """已知路由正常返回 (user_id, session_id)"""
    router = ContextRouter({"cli:default": "user-alice"})
    user_id, session_id = router.resolve("cli", "default")
    assert user_id == "user-alice"
    assert session_id == "cli:default"


def test_route_unknown_channel():
    """未知路由抛出 UnknownRouteError"""
    router = ContextRouter({})
    with pytest.raises(UnknownRouteError):
        router.resolve("cli", "unknown")


def test_route_override():
    """显式 override 优先级最高"""
    router = ContextRouter({"cli:default": "user-alice"})
    user_id, session_id = router.resolve("cli", "default", override="user-bob")
    assert user_id == "user-bob"
    # override 不影响 session_id 派生
    assert session_id == "cli:default"


def test_route_wildcard():
    """通配符 channel:* 匹配"""
    router = ContextRouter({"cli:*": "user-alice"})
    assert _uid(router.resolve("cli", "anything")) == "user-alice"
    assert _uid(router.resolve("cli", "default")) == "user-alice"


def test_route_exact_before_wildcard():
    """精确匹配优先于通配匹配"""
    router = ContextRouter({
        "cli:default": "user-alice",
        "cli:*": "user-shared",
    })
    assert _uid(router.resolve("cli", "default")) == "user-alice"
    assert _uid(router.resolve("cli", "other")) == "user-shared"


def test_set_route():
    """动态设置路由"""
    router = ContextRouter({})
    router.set_route("http", "chat-1", "user-bob")
    assert _uid(router.resolve("http", "chat-1")) == "user-bob"


def test_remove_route():
    """移除路由"""
    router = ContextRouter({"cli:default": "user-alice"})
    assert router.remove_route("cli", "default") is True
    with pytest.raises(UnknownRouteError):
        router.resolve("cli", "default")


def test_remove_nonexistent_route():
    """移除不存在的路由返回 False"""
    router = ContextRouter({})
    assert router.remove_route("cli", "unknown") is False


def test_load_from_config():
    """从配置加载路由表"""
    router = ContextRouter()
    router.load_from_config({
        "cli:default": "user-alice",
        "http:chat-123": "user-bob",
    })
    assert _uid(router.resolve("cli", "default")) == "user-alice"
    assert _uid(router.resolve("http", "chat-123")) == "user-bob"


def test_mapping_readonly():
    """mapping 返回只读副本"""
    router = ContextRouter({"cli:default": "user-alice"})
    mapping = router.mapping
    mapping["new"] = "hacked"
    assert "new" not in router.mapping


def test_repr():
    """repr 包含路由数"""
    router = ContextRouter({"a:b": "u1", "c:d": "u2"})
    rep = repr(router)
    assert "routes=2" in rep


def test_different_channels_same_chat_id():
    """不同 channel 的相同 chat_id 可映射到不同用户"""
    router = ContextRouter({
        "cli:default": "user-alice",
        "http:default": "user-bob",
    })
    assert _uid(router.resolve("cli", "default")) == "user-alice"
    assert _uid(router.resolve("http", "default")) == "user-bob"
