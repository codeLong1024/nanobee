"""
通道消息模型 — ChannelMessage / OutboundMessage / 流式增量数据类。

所有通道插件使用这些统一的模型与内核交换数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChannelMessage:
    """统一的入站通道消息模型。

    Attributes:
        channel:     通道名（如 cli、wechat、discord）
        sender_id:   发送方标识（用户ID / 微信 openid 等）
        chat_id:     会话标识（cli 用固定值，IM 用 group/private id）
        content:     消息文本
        media:       附件路径或 URL 列表
        metadata:    补充元数据（可为通道特有的额外字段）
    """

    channel: str
    sender_id: str
    chat_id: str
    content: str = ""
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def context_id(self) -> str:
        """返回上下文管理器用的 context_id。"""
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """统一出站消息模型，包括纯文本、媒体、流式增量。"""

    channel: str
    chat_id: str
    content: str = ""
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamingDelta:
    """流式消息增量，由 send_delta 的生成器产生。

    Attributes:
        content:        文本增量（可能为空）
        finish_reason:  结束原因，非空时标志着本轮流式终止
        reasoning:      推理过程增量（model-provider 产出的 CoT 文本，选填）
    """

    content: str = ""
    finish_reason: str | None = None
    reasoning: str | None = None


__all__ = [
    "ChannelMessage",
    "OutboundMessage",
    "StreamingDelta",
]
