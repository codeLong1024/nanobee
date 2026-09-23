"""CLI 通道插件实现"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from nanobee.channel.base import ChannelPlugin
from nanobee.outbound import OutboundMessage
from nanobee.utils.logger import logger


class ChannelCliConfig(BaseModel):
    """channel_cli 插件声明式配置。

    Attributes:
        prompt_prefix: 出站消息前缀。
        input_prefix: 输入提示符前缀。
    """

    prompt_prefix: str = "🐝 "
    input_prefix: str = "你: "


class ChannelCLIPlugin(ChannelPlugin):
    """命令行交互通道"""

    config_cls = ChannelCliConfig

    display_name = "命令行"
    safe_for_gateway = False

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ====== 生命周期 ======

    async def start(self) -> None:
        """启动 CLI 通道（开始接收用户输入）"""
        self._running = True
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
        """发送完整出站消息到 CLI。

        终端没有附件投递能力：``message.media`` 非空时只记 debug（已知取舍），
        不改变终端输出形态，也不影响正文打印。
        """
        prefix = self.config.prompt_prefix
        if message.media:
            logger.debug(
                "CLI 通道忽略 {} 个附件（终端无附件投递能力）: {}",
                len(message.media), message.media,
            )
        if message.content:
            print(f"\n{prefix}{message.content}")

    # ====== 交互循环 ======

    async def _interaction_loop(self) -> None:
        """交互循环（读取用户输入并直连内核处理）"""
        loop = asyncio.get_event_loop()
        prefix_prompt = self.config.input_prefix
        # 与历史入站路径保持一致：context_id = <channel>:<chat_id>
        context_id = f"{self.metadata.name}:default"

        while self._running:
            try:
                user_input = await loop.run_in_executor(None, input, prefix_prompt)

                if not self._running:
                    break

                content = user_input.strip()
                if content == "/exit":
                    self._running = False
                    break

                if self.kernel is None:
                    logger.warning("内核未初始化，无法处理消息")
                    await self.send(
                        OutboundMessage(
                            channel=self.metadata.name,
                            chat_id="default",
                            content="[内核未就绪]",
                        ),
                        context_id,
                    )
                    continue

                async def _on_progress(delta: str, *, tool_hint: bool = False,
                                       tool_events: list[dict] | None = None) -> None:
                    if tool_hint:
                        print("\n🔧 正在调用工具...", flush=True)

                response = await self.kernel.handle_message(
                    content, context_id,
                    channel=self.metadata.name,
                    session_id="cli:direct",
                    on_progress=_on_progress,
                )
                if response and response.content:
                    await self.send(
                        OutboundMessage(
                            channel=self.metadata.name,
                            chat_id="default",
                            content=response.content,
                        ),
                        context_id,
                    )

            except EOFError:
                self._running = False
                break
            except Exception as e:
                logger.exception(f"CLI 交互循环出错: {e}")

        logger.info("CLI 交互循环已退出")


__all__ = [
    "ChannelCLIPlugin",
]
