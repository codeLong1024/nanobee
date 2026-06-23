"""CLI 通道插件实现"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobee.channel.base import ChannelPlugin
from nanobee.channel.message import ChannelMessage, OutboundMessage, StreamingDelta
from nanobee.kernel.context_manager import ContextManager

from nanobee.utils.logger import logger



class ChannelCLIPlugin(ChannelPlugin):
    """命令行交互通道"""

    name = "channel_cli"
    version = "1.0.0"
    display_name = "命令行"
    supports_streaming = True
    safe_for_gateway = False

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._context_manager: ContextManager | None = None

    # ====== 生命周期 ======

    async def start(self) -> None:
        """启动 CLI 通道（开始接收用户输入）"""
        self._running = True
        if self.kernel is not None:
            self._context_manager = self.kernel.context_manager
        logger.info("CLI 通道已启动")
        self._task = asyncio.create_task(self._interaction_loop())

    async def stop(self) -> None:
        """停止 CLI 通道"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("CLI 通道已停止")

    # ====== 发送实现 ======

    async def send(self, message: OutboundMessage, context_id: str = "default") -> None:
        """发送完整出站消息到 CLI。"""
        prefix = self.get_config("prompt_prefix", "🐝 ")
        if message.content:
            print(f"\n{prefix}{message.content}")

    async def send_delta(
        self,
        delta: StreamingDelta,
        context_id: str = "default",
    ) -> None:
        """流式发送消息增量到 CLI（逐字打印）。"""
        if delta.reasoning:
            # 推理过程用灰色风格（终端 ANSI 转义）
            print(f"\033[90m{delta.reasoning}\033[0m", end="", flush=True)
        if delta.content:
            print(delta.content, end="", flush=True)
        if delta.finish_reason is not None:
            print()  # 换行

    async def send_reasoning_delta(
        self, reasoning: str, context_id: str = "default"
    ) -> None:
        """流式发送推理过程增量（灰色显示）。"""
        print(f"\033[90m{reasoning}\033[0m", end="", flush=True)

    async def send_reasoning_end(self, context_id: str = "default") -> None:
        """标记推理过程结束。"""
        print("\033[0m")  # 重置颜色

    # ====== 消息处理 ======

    async def _process_incoming(
        self,
        message: ChannelMessage,
        context_manager: ContextManager,
    ) -> list[OutboundMessage]:
        """处理 CLI 用户输入并返回回复。

        注意：由于 CLI 是同步流式的，此处将消息交给内核处理后直接输出。
        当前实现简化：如果内核可用则通过 handle_message 处理。
        """
        content = message.content.strip()
        if content == "/exit":
            self._running = False
            return []

        if self.kernel is not None:
            async def _on_progress(delta: str, *, tool_hint: bool = False,
                                   tool_events: list[dict] | None = None) -> None:
                if tool_hint:
                    print("\n🔧 正在调用工具...", flush=True)

            response = await self.kernel.handle_message(
                content, message.context_id,
                channel=self.metadata.name,
                session_id="cli:direct",
                on_progress=_on_progress,
            )
            content_text = response.content if response else ""
            return [
                OutboundMessage(
                    channel=self.metadata.name,
                    chat_id=message.chat_id,
                    content=content_text,
                )
            ]
        else:
            return [
                OutboundMessage(
                    channel=self.metadata.name,
                    chat_id=message.chat_id,
                    content="[内核未就绪]",
                )
            ]

    # ====== 交互循环 ======

    async def _interaction_loop(self) -> None:
        """交互循环（读取用户输入并转发给内核）"""
        loop = asyncio.get_event_loop()
        prefix_prompt = self.get_config("input_prefix", "你: ")

        while self._running:
            try:
                user_input = await loop.run_in_executor(None, input, prefix_prompt)

                if not self._running:
                    break

                if user_input.strip() == "/exit":
                    self._running = False
                    break

                # 构造统一的 ChannelMessage
                msg = ChannelMessage(
                    channel=self.metadata.name,
                    sender_id="user",
                    chat_id="default",
                    content=user_input,
                )

                # 通过 handle_incoming 走统一入口
                if self.kernel is not None:
                    cm = self.kernel.context_manager
                    replies = await self.handle_incoming(msg, cm)
                    for reply in replies:
                        if reply.content:
                            await self.send(reply, msg.context_id)
                else:
                    logger.warning("内核未初始化，无法处理消息")

            except EOFError:
                self._running = False
                break
            except Exception as e:
                logger.exception(f"CLI 交互循环出错: {e}")

        logger.info("CLI 交互循环已退出")


__all__ = [
    "ChannelCLIPlugin",
]
