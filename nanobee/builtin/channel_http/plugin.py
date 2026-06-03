"""
Channel HTTP 插件 - HTTP 通信渠道

TODO: 实现完整的 HTTP/WebSocket 服务。
"""

from __future__ import annotations

import logging
from typing import Any

from nanobee.channel.base import ChannelPlugin
from nanobee.channel.message import ChannelMessage, OutboundMessage
from nanobee.kernel.context_manager import ContextManager

logger = logging.getLogger(__name__)


class HTTPChannelPlugin(ChannelPlugin):
    """HTTP 渠道插件（存根）。"""

    name = "channel_http"
    version = "0.0.1"
    display_name = "HTTP"

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._server: Any = None

    async def start(self) -> None:
        """启动 HTTP 服务（TODO 实现）"""
        logger.info("HTTP 通道启动（存根）")

    async def stop(self) -> None:
        """停止 HTTP 服务（TODO 实现）"""
        logger.info("HTTP 通道停止（存根）")

    async def send(self, message: OutboundMessage, context_id: str = "default") -> None:
        """发送出站消息（TODO 实现）"""
        pass

    async def _process_incoming(
        self,
        message: ChannelMessage,
        context_manager: ContextManager,
    ) -> list[OutboundMessage]:
        """处理入站消息（TODO 实现）"""
        logger.warning("HTTP 通道尚未实现 _process_incoming")
        return []


__all__ = [
    "HTTPChannelPlugin",
]
