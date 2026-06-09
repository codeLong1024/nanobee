"""消息数据模型 — 入站和出站消息定义。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """来自聊天通道的消息。"""

    channel: str
    sender_id: str
    chat_id: str
    content: str
    timestamp: Any = field(default_factory=time.time)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    context_id_override: str | None = None

    @property
    def context_id(self) -> str:
        """获取上下文 ID。

        优先级:
        1. context_id_override 显式指定
        2. sender_id (用户唯一标识,如钉钉 staffId)
        3. channel:chat_id (兜底兼容)

        参考 nanobot_channel_dingtalk 的设计,使用 sender_id 作为唯一标识,
        避免创建重复的用户目录。
        """
        if self.context_id_override:
            return self.context_id_override
        if self.sender_id:
            return self.sender_id
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """发送到聊天通道的消息。"""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
