"""CLI 通道插件实现"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from nanobee.plugins.channel import ChannelPlugin

logger = logging.getLogger(__name__)


class ChannelCLIPlugin(ChannelPlugin):
    """命令行交互通道"""

    name = "channel_cli"
    version = "1.0.0"

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._running = False
        self._message_handler: Callable | None = None

    async def start(self) -> None:
        """启动 CLI 通道（开始接收用户输入）"""
        self._running = True
        logger.info("CLI 通道已启动")

        # 在新任务中运行交互循环，保存引用以便后续清理
        self._task = asyncio.create_task(self._interaction_loop())

    async def stop(self) -> None:
        """停止 CLI 通道"""
        self._running = False
        if hasattr(self, "_task") and self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("CLI 通道已停止")

    async def send(self, message: str, **kwargs: Any) -> None:
        """发送消息到 CLI

        Args:
            message: 消息内容
        """
        prefix = self.get_config("prompt_prefix", "🐝 ")
        print(f"\n{prefix}{message}")

    async def _interaction_loop(self) -> None:
        """交互循环（读取用户输入并转发给内核）"""
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # 在 executor 中运行 input() 避免阻塞事件循环
                user_input = await loop.run_in_executor(None, input, "你: ")

                if not self._running:
                    break

                if user_input.strip() == "/exit":
                    self._running = False
                    break

                # 转发给内核处理
                if self._message_handler:
                    response = await self._message_handler(user_input, {"channel": "cli"})
                    await self.send(response)

            except EOFError:
                self._running = False
                break
            except Exception as e:
                logger.exception(f"CLI 交互循环出错: {e}")

        logger.info("CLI 交互循环已退出")
