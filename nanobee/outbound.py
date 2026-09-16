"""统一出站契约 — 出站消息模型、事件名、载荷构造与发布原语。

本模块是「出站信息」的唯一契约归属：agent 侧与 channel 侧都从这里取模型，
任何出站事件的载荷都由 :func:`outbound_payload` 构造、由
:func:`publish_outbound` 发布。新增出站字段只需改本模块一处，
「某个发布者漏字段导致静默丢数据」在结构上不再可能。

消费方约定：``agent.outbound`` 载荷中 ``media`` 为**可选**字段（缺省即空列表），
语义同 :class:`OutboundMessage` —— 本地绝对路径或 http(s) URL；不带该字段的
载荷按空列表处理，因此新增字段对既有消费方向后兼容。

依赖纪律：本模块是叶子模块，运行时仅依赖标准库（``dataclasses``/``typing``），
``EventBus`` 仅用于类型注解（TYPE_CHECKING），避免任何一侧导入时形成环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobee.events.event_bus import EventBus


OUTBOUND_EVENT: str = "agent.outbound"
"""出站事件名（发布方与订阅方共用同一字符串）。"""


@dataclass
class OutboundMessage:
    """统一出站消息模型（出站契约的唯一归属）。

    Attributes:
        channel: 目标通道名（如 cli / channel_dingtalk）。
        chat_id: 会话标识（群 ID 或用户 ID，由目标通道自行解释）。
        content: 正文文本，默认空字符串。
        reply_to: 可选的消息引用 ID。
        media: 附件引用列表（本地绝对路径或 http(s) URL），默认空。
        metadata: 通道自解释的附加元数据（系统通知标记等）。
    """

    channel: str
    chat_id: str
    content: str = ""
    # 当前不进 ``agent.outbound`` 载荷（无生产者赋值、无消费者读取）：纳入前
    # 必须先扩展 :func:`outbound_payload` 的键集合，否则赋值会被唯一构造点静默丢弃。
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _normalize_media(media: Any) -> list[str]:
    """把任意 media 输入归一化为 ``list[str]``。

    契约不变量：载荷中的 ``media`` 恒为字符串列表。非序列输入（None、Mock、
    字符串等）视为空列表；序列中的非字符串项逐个丢弃——消费方因此无需再做
    类型防御，也不会因发布方传入脏数据而抛异常。

    Args:
        media: 待归一化的媒体引用集合（可为 None / list / tuple / 任意对象）。

    Returns:
        归一化后的媒体引用列表（保持原顺序）。
    """
    if not isinstance(media, (list, tuple)):
        return []
    return [item for item in media if isinstance(item, str)]


def outbound_payload(msg: OutboundMessage) -> dict[str, Any]:
    """构造 ``agent.outbound`` 事件载荷（唯一构造点）。

    载荷键集合在此唯一确定：``channel`` / ``chat_id`` / ``content`` /
    ``media`` / ``metadata``。新增出站字段只需改这里一处，所有发布者自动生效。

    Args:
        msg: 出站消息契约对象。

    Returns:
        事件载荷字典；``media`` 已归一化为字符串列表，``metadata`` 为浅拷贝
        （调用方后续改动不影响已发布的载荷）。
    """
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    return {
        "channel": msg.channel,
        "chat_id": msg.chat_id,
        "content": msg.content,
        "media": _normalize_media(msg.media),
        "metadata": dict(metadata),
    }


async def publish_outbound(event_bus: EventBus | None, msg: OutboundMessage) -> None:
    """发布出站事件（唯一发布原语）。

    ``event_bus`` 不可用（内核未就绪）时静默跳过：与既有各调用点的守卫语义
    一致（cron 侧「无总线视为跳过成功」，loop 侧仅在 event_bus 存在时订阅
    该处理器），避免为「总线缺失」这一非错误状态引入噪声日志。
    发布过程中的异常不在此吞掉，由调用方决定降级策略（如 cron 区分
    「执行失败」与「投递失败」）。

    Args:
        event_bus: 事件总线；为 None 时不发布。
        msg: 出站消息契约对象。
    """
    if event_bus is None:
        return
    await event_bus.publish(OUTBOUND_EVENT, outbound_payload(msg))


__all__ = [
    "OUTBOUND_EVENT",
    "OutboundMessage",
    "outbound_payload",
    "publish_outbound",
]
