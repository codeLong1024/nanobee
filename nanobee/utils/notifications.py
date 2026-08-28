"""统一消息目录 — 管理所有框架级用户可见消息。

单文件真相来源：所有命令响应、异常通知、max_iterations 终止消息均在此定义。
遵循框架无知论：目录只提供消息内容（数据字典），不做任何策略决策。
提供两个工厂函数：
- build_notification() → OutboundMessage: 用于即时通知（命令响应、异常消息）
- get_notification_content() → str: 用于仅需内容的场景（max_iterations 追加到 session 历史）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobee.agent.messages import OutboundMessage


@dataclass(frozen=True)
class Notification:
    """统一通知定义 — 框架级用户可见消息的元数据载体。

    Attributes:
        kind: 通知类型标识（如 "command_new"、"turn_cancelled"）。
        content: 消息内容模板，支持 Python .format() 占位符。
        severity: 严重程度（info / warning / error），供通道渲染差异化展示。
    """

    kind: str
    content: str
    severity: str = "info"


# ── 消息目录 ────────────────────────────────────────────────────────────
# 添加新消息只需追加一行，集中管理，不散落为独立模板文件。

_CATALOG: dict[str, Notification] = {
    # ── Slash 命令响应 ──
    "command_new": Notification(
        kind="command_new",
        content="会话已重置。下一条消息将开始全新对话。",
    ),
    "command_stop": Notification(
        kind="command_stop",
        content="已发送停止信号，当前任务将被取消。",
    ),
    "command_stop_idle": Notification(
        kind="command_stop_idle",
        content="当前没有正在运行的任务。",
        severity="warning",
    ),
    "command_status": Notification(
        kind="command_status",
        content="**运行时状态**\n- 用户: {user_id}\n- 会话: {session_id}\n- 消息数: {msg_count}\n- 当前状态: {turn_status}\n- 当前锁定用户: {locked_users}",
    ),
    "command_help": Notification(
        kind="command_help",
        content="**可用命令**\n\n{command_list}",
    ),
    "command_failed": Notification(
        kind="command_failed",
        content="命令 {cmd} 执行失败，请稍后重试。",
        severity="error",
    ),
    # ── Turn 异常通知 ──
    "turn_cancelled": Notification(
        kind="turn_cancelled",
        content="任务已被取消。",
        severity="warning",
    ),
    "turn_internal_error": Notification(
        kind="turn_internal_error",
        content=(
            "抱歉，处理消息时发生内部错误，请稍后重试或联系管理员。\n\n"
            "详细信息：\n{detail}\n\n"
            "如问题持续，请重试一次；若仍失败，可尝试换一种提问方式。"
        ),
        severity="error",
    ),
    # ── Agent 循环终止 ──
    "turn_max_iterations": Notification(
        kind="turn_max_iterations",
        content="对话因超出最大迭代次数（{max_iterations} 次）而自动终止。如有需要，请重新发起提问。",
        severity="warning",
    ),
    # ── 子代理通知 ──
    "subagent_spawned": Notification(
        kind="subagent_spawned",
        content="已启动子代理 **{label}**（ID: `{task_id}`）\n\n> {task_preview}",
    ),
}


def get_notification(kind: str) -> Notification:
    """获取通知定义。

    Args:
        kind: 通知类型标识（如 "command_new"）。

    Returns:
        对应的 Notification 数据对象。

    Raises:
        KeyError: kind 在目录中不存在。
    """
    return _CATALOG[kind]


def get_notification_content(kind: str, **kwargs: Any) -> str:
    """获取格式化后的通知内容（仅返回文本，不构造 OutboundMessage）。

    适用于需要把内容注入到消息历史（如 max_iterations）而非直接回复的场景。

    Args:
        kind: 通知类型标识。
        **kwargs: 替换 content 模板中的 {key} 占位符。

    Returns:
        格式化后的纯文本内容。
    """
    notif = _CATALOG[kind]
    return notif.content.format(**kwargs)


def build_notification(
    kind: str,
    channel: str,
    chat_id: str,
    **kwargs: Any,
) -> "OutboundMessage":
    """构建框架级通知的 OutboundMessage。

    用于命令响应、异常消息等直接回复给用户的场景。
    metadata 中包含 notification_type、notification_kind、severity，
    通道可通过这些字段统一识别并差异化渲染系统通知。

    Args:
        kind: 通知类型标识（如 "command_new"、"turn_cancelled"）。
        channel: 目标通道名。
        chat_id: 目标会话 ID。
        **kwargs: 替换 content 模板中的 {key} 占位符，以及追加到 metadata 的额外字段。

    Returns:
        填充好 content 和 metadata 的 OutboundMessage。
    """
    from nanobee.agent.messages import OutboundMessage

    notif = _CATALOG[kind]
    content = notif.content.format(**kwargs)
    return OutboundMessage(
        channel=channel,
        chat_id=chat_id,
        content=content,
        metadata={
            "notification_type": "system",
            "notification_kind": notif.kind,
            "severity": notif.severity,
        },
    )


# 公开展示消息清单（供调试/测试使用）
def list_kinds() -> list[str]:
    """返回所有已注册的通知类型标识列表。"""
    return sorted(_CATALOG.keys())
