"""
通道消息模型 — ChannelMessage 与出站模型 re-export。

所有通道插件使用这些统一的模型与内核交换数据。
出站模型（``OutboundMessage``）的唯一归属是 :mod:`nanobee.outbound`，
本模块仅作 re-export 保持旧 import 路径兼容。

注：原 ``StreamingDelta`` 与 ``send_delta`` / ``send_reasoning_delta`` /
``send_reasoning_end`` 通道接口已删除（2026-09-16）——全仓零构造、零调用，
流式实际走 ``on_stream`` / ``on_stream_end`` 回调。历史实现若 import
``StreamingDelta``，需改用自身的数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobee.outbound import OutboundMessage


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


__all__ = [
    "ChannelMessage",
    "OutboundMessage",
]
