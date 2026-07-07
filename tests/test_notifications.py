"""统一消息目录 Notification 单元测试。

覆盖场景：
1. get_notification 获取通知定义
2. get_notification_content 格式化通知内容
3. build_notification 构造 OutboundMessage
4. list_kinds 列出所有通知类型
5. 未知 kind 异常处理
6. 所有已注册通知类型的完整性
"""

from __future__ import annotations

import pytest

from nanobee.utils.notifications import (
    Notification,
    build_notification,
    get_notification,
    get_notification_content,
    list_kinds,
)


class TestNotificationDataClass:
    """Notification 数据类测试。"""

    def test_default_severity(self) -> None:
        """默认严重程度为 info。"""
        notif = Notification(kind="test_kind", content="测试内容")
        assert notif.kind == "test_kind"
        assert notif.content == "测试内容"
        assert notif.severity == "info"

    def test_custom_severity(self) -> None:
        """可指定严重程度。"""
        notif = Notification(kind="err", content="错误", severity="error")
        assert notif.severity == "error"

    def test_frozen(self) -> None:
        """Notification 是不可变的。"""
        notif = Notification(kind="k", content="c")
        with pytest.raises(Exception):
            notif.kind = "new"  # type: ignore[misc]


class TestGetNotification:
    """get_notification() 测试。"""

    def test_get_known_kind(self) -> None:
        """已知类型正常返回。"""
        notif = get_notification("command_new")
        assert notif.kind == "command_new"
        assert "会话已重置" in notif.content

    def test_get_unknown_kind_raises(self) -> None:
        """未知类型抛出 KeyError。"""
        with pytest.raises(KeyError):
            get_notification("nonexistent_kind")


class TestGetNotificationContent:
    """get_notification_content() 测试。"""

    def test_format_simple_content(self) -> None:
        """无占位符时直接返回内容。"""
        content = get_notification_content("command_new")
        assert "会话已重置" in content

    def test_format_with_placeholders(self) -> None:
        """有占位符时正确填充。"""
        content = get_notification_content(
            "command_status",
            user_id="alice",
            session_id="dingtalk:chat1",
            msg_count="42",
            turn_status="空闲等待",
            locked_users="无",
        )
        assert "alice" in content
        assert "dingtalk:chat1" in content
        assert "42" in content
        assert "空闲等待" in content

    def test_format_unknown_kind_raises(self) -> None:
        """未知类型抛出 KeyError。"""
        with pytest.raises(KeyError):
            get_notification_content("bad_kind")

    def test_format_max_iterations(self) -> None:
        """turn_max_iterations 通知正确格式化。"""
        content = get_notification_content("turn_max_iterations", max_iterations=10)
        assert "10" in content
        assert "最大迭代次数" in content


class TestBuildNotification:
    """build_notification() 测试。"""

    def test_build_command_new(self) -> None:
        """构造 command_new 的 OutboundMessage。"""
        msg = build_notification("command_new", channel="cli", chat_id="user-1")
        assert msg.channel == "cli"
        assert msg.chat_id == "user-1"
        assert "会话已重置" in msg.content
        assert msg.metadata["notification_type"] == "system"
        assert msg.metadata["notification_kind"] == "command_new"
        assert msg.metadata["severity"] == "info"

    def test_build_command_status_with_args(self) -> None:
        """构造带参数的 command_status OutboundMessage。"""
        msg = build_notification(
            "command_status",
            channel="dingtalk",
            chat_id="chat-1",
            user_id="bob",
            session_id="dingtalk:chat-1",
            msg_count="5",
            turn_status="运行中",
            locked_users="bob",
        )
        assert msg.channel == "dingtalk"
        assert "bob" in msg.content
        assert "5" in msg.content
        assert "运行中" in msg.content

    def test_build_turn_cancelled(self) -> None:
        """turn_cancelled 的严重程度为 warning。"""
        msg = build_notification("turn_cancelled", channel="cli", chat_id="u1")
        assert msg.metadata["severity"] == "warning"
        assert "已被取消" in msg.content

    def test_build_unknown_kind_raises(self) -> None:
        """未知类型抛出 KeyError。"""
        with pytest.raises(KeyError):
            build_notification("ghost_kind", channel="cli", chat_id="u1")


class TestListKinds:
    """list_kinds() 测试。"""

    def test_returns_sorted_list(self) -> None:
        """返回排序后的通知类型列表。"""
        kinds = list_kinds()
        assert isinstance(kinds, list)
        assert len(kinds) > 0
        assert kinds == sorted(kinds)

    def test_contains_all_expected_kinds(self) -> None:
        """包含所有预期的通知类型。"""
        kinds = list_kinds()
        expected = [
            "command_failed",
            "command_help",
            "command_new",
            "command_status",
            "command_stop",
            "command_stop_idle",
            "subagent_spawned",
            "turn_cancelled",
            "turn_internal_error",
            "turn_max_iterations",
        ]
        for kind in expected:
            assert kind in kinds, f"缺少通知类型: {kind}"


class TestNotificationCatalogIntegrity:
    """通知目录完整性测试。"""

    def test_all_notifications_have_non_empty_content(self) -> None:
        """所有通知的内容字段非空。"""
        for kind in list_kinds():
            notif = get_notification(kind)
            assert notif.content, f"{kind} 的 content 为空"
            assert notif.content.strip(), f"{kind} 的 content 为空白"

    def test_all_notifications_have_valid_severity(self) -> None:
        """所有通知的 severity 为 info / warning / error。"""
        for kind in list_kinds():
            notif = get_notification(kind)
            assert notif.severity in ("info", "warning", "error"), (
                f"{kind} 的 severity 值无效: {notif.severity}"
            )
