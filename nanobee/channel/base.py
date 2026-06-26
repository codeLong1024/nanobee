"""
Channel Plugin 基类 — 所有通讯通道插件必须继承此基类。

增强点：
1. 新增消息模型 ChannelMessage / OutboundMessage / StreamingDelta
2. 流式接口 send_delta / send_reasoning_delta / send_reasoning_end
3. 权限校验 pairing_code 机制
4. 配置属性 supports_streaming / display_name
5. _handle_incoming 自动权限检查与流式标记注入
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator

from nanobee.channel.message import ChannelMessage, OutboundMessage, StreamingDelta
from nanobee.kernel.context_manager import ContextManager
from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.logger import logger



class ChannelPlugin(NanobeePlugin, ABC):
    """通道插件基类。所有通讯通道（CLI、HTTP、WebSocket、IM 等）必须继承此类。"""

    # ====== 插件类型标记 ======
    plugin_type: str = "channel"

    # ====== 配置属性 ======
    supports_streaming: bool = False
    """该通道是否支持流式输出。子类可覆盖为 True。"""

    display_name: str = ""
    """通道展示名，例如「命令行」「WebSocket」，默认取 metadata.name。"""

    safe_for_gateway: bool = True
    """该通道是否适合在 Gateway 模式下自动启动。
    交互式通道（如 CLI）应设为 False。"""

    # ====== 权限 / 配对 ======
    pairing_code: str | None = None
    """可选的配对码，用于限制通道只能在持有该配对码的客户端上使用。
    为 None 时不校验。"""

    def is_allowed(self, pairing_code: str | None) -> bool:
        """校验配对码。

        Returns:
            True 表示允许该客户端接入。
        """
        if self.pairing_code is None:
            return True
        return pairing_code == self.pairing_code

    # ====== 生命周期 ======
    def on_load(self) -> None:
        """通道插件加载时自注册到内核。"""
        if not self.display_name:
            self.display_name = self.metadata.name
        logger.info("通道 {} ({}) 已加载", self.display_name, self.metadata.plugin_type)

    def on_enable(self) -> None:
        """通道插件启用时，订阅 agent.outbound 事件以接收出站消息。

        出站消息来源包括：
        - AgentLoop 正常回复（通过 _dispatch 发布）
        - Cron 插件定时任务触发（通过 _on_job_execute 发布）

        订阅处理器根据事件中的 channel 字段匹配当前通道，
        匹配成功则调用 send() 投递给用户。
        """
        super().on_enable()
        if self.kernel and self.kernel.event_bus:
            self.kernel.event_bus.subscribe("agent.outbound", self._on_agent_outbound)
            logger.debug("通道 {} 已订阅 agent.outbound 事件", self.display_name)

    def on_disable(self) -> None:
        """禁用时取消事件订阅，避免重复订阅或残留 handler。"""
        self._unsubscribe_agent_outbound()
        super().on_disable()

    def on_unload(self) -> None:
        """卸载前先取消订阅，再释放内核引用。"""
        self._unsubscribe_agent_outbound()
        super().on_unload()

    def _unsubscribe_agent_outbound(self) -> None:
        """取消 agent.outbound 事件订阅（内部辅助方法）。"""
        if self.kernel and self.kernel.event_bus:
            self.kernel.event_bus.unsubscribe("agent.outbound", self._on_agent_outbound)
            logger.debug("通道 {} 已取消订阅 agent.outbound 事件", self.display_name)

    async def _on_agent_outbound(self, data: dict) -> None:
        """处理 agent.outbound 事件：匹配通道后投递消息。

        Args:
            data: 事件数据，包含 channel、chat_id、content、metadata
        """
        if not isinstance(data, dict):
            return
        channel_name = data.get("channel", "")
        if channel_name != self.metadata.name:
            return
        chat_id = data.get("chat_id", "direct")
        content = data.get("content", "")
        if not content:
            return
        msg = OutboundMessage(
            channel=channel_name,
            chat_id=chat_id,
            content=content,
            metadata=data.get("metadata", {}),
        )
        await self.send(msg, context_id=chat_id)

    # ====== 抽象方法 ======
    @abstractmethod
    async def send(
        self, message: OutboundMessage, context_id: str = "default"
    ) -> None:
        """发送完整的出站消息（非流式）。"""
        ...

    async def send_delta(
        self,
        delta: StreamingDelta,
        context_id: str = "default",
    ) -> None:
        """流式发送消息增量。默认回退为完整消息发送，子类应当覆盖以实现真正流式。

        Arguments:
            delta:     流式增量数据
            context_id: 上下文 ID
        """
        if delta.finish_reason is not None:
            # 流结束，发送剩余内容
            if delta.content:
                await self.send(
                    OutboundMessage(
                        channel=self.metadata.name,
                        chat_id=context_id.split(":", 1)[-1],
                        content=delta.content,
                    ),
                    context_id=context_id,
                )

    async def send_reasoning_delta(
        self, reasoning: str, context_id: str = "default"
    ) -> None:
        """流式发送推理过程增量。默认 no-op，子类按需覆盖。"""
        ...

    async def send_reasoning_end(self, context_id: str = "default") -> None:
        """标记推理过程结束。默认 no-op，子类按需覆盖。"""
        ...

    # ====== 公共入口（内核调用） ======
    async def handle_incoming(
        self,
        message: ChannelMessage,
        context_manager: ContextManager,
        pairing_code: str | None = None,
    ) -> list[OutboundMessage]:
        """处理入站消息前的权限检查与流式标记注入。

        子类应当实现 _process_incoming（或覆盖本方法）来完成实际业务逻辑。

        Returns:
            回复消息列表（非流式模式时返回）。
        """
        if not self.is_allowed(pairing_code):
            logger.warning("通道 {} 拒绝未授权连接（pairing_code={}）", self.metadata.name, pairing_code)
            return [
                OutboundMessage(
                    channel=self.metadata.name,
                    chat_id=message.chat_id,
                    content="Connection not allowed. Invalid or missing pairing code.",
                )
            ]

        # 注入流式标记到 metadata，下游 agent 可根据此标记决定是否调用流式方法
        if self.supports_streaming:
            message.metadata["_stream_supported"] = True

        return await self._process_incoming(message, context_manager)

    @abstractmethod
    async def _process_incoming(
        self,
        message: ChannelMessage,
        context_manager: ContextManager,
    ) -> list[OutboundMessage]:
        """子类实现的真正消息处理逻辑。"""
        ...


__all__ = [
    "ChannelPlugin",
]
