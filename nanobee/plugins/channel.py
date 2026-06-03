"""ChannelPlugin 接口 - 通道插件"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any, Callable

from nanobee.plugins.base import NanobeePlugin

logger = logging.getLogger(__name__)


class ChannelPlugin(NanobeePlugin):
    """通道插件基类

    通道是 Agent 与外部世界交互的桥梁（CLI、HTTP、Telegram、飞书等）。
    """

    plugin_type = "channel"

    @abstractmethod
    async def start(self) -> None:
        """启动通道（开始接收消息）"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止通道"""
        ...

    @abstractmethod
    async def send(self, message: str, **kwargs: Any) -> None:
        """发送消息到通道

        Args:
            message: 消息内容
            **kwargs: 通道特定的参数（如 chat_id、thread_id 等）
        """
        ...

    def on_message(self, handler: Callable[[str, dict], Any]) -> None:
        """注册消息处理器

        Args:
            handler: 回调函数，接收 (message, metadata) 两个参数
        """
        self._message_handler = handler

    async def _handle_incoming(self, message: str, metadata: dict | None = None) -> None:
        """处理 incoming 消息（由通道实现调用）"""
        if hasattr(self, "_message_handler"):
            await self._message_handler(message, metadata or {})
