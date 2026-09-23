"""
Channel Plugin 基类 — 所有通讯通道插件必须继承此基类。

入站：通道自行接收外部消息并直连 ``kernel.handle_message()``，基类不参与路由。

已删除（2026-09-16）：
1. ``send_delta`` / ``send_reasoning_delta`` / ``send_reasoning_end`` 与
   ``StreamingDelta``——全仓零调用、零构造，流式实际走 ``on_stream`` /
   ``on_stream_end`` 回调（见各通道 ``_make_stream_callback``）。
2. ``handle_incoming`` / ``_process_incoming`` 入站旁路与 ``ChannelMessage``——
   该链路从不承载生产流量（唯一调用点在永不启动的 CLI 交互循环内）。
3. ``supports_streaming`` / ``_stream_supported`` 标记与 ``pairing_code`` /
   ``is_allowed`` 校验——前者 write-only（写入后无人读取），后者生产零赋值、
   恒为 no-op；二者都只服务于已删除的入站旁路。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nanobee.outbound import OutboundMessage
from nanobee.plugins.base import NanobeePlugin
from nanobee.utils.logger import logger


class ChannelPlugin(NanobeePlugin, ABC):
    """通道插件基类。所有通讯通道（CLI、HTTP、WebSocket、IM 等）必须继承此类。"""

    # ====== 插件类型标记 ======
    plugin_type: str = "channel"

    # ====== 配置属性 ======
    display_name: str = ""
    """通道展示名，例如「命令行」「WebSocket」，默认取 metadata.name。"""

    safe_for_gateway: bool = True
    """该通道是否适合在 Gateway 模式下自动启动。
    交互式通道（如 CLI）应设为 False。"""

    supports_push: bool = True
    """该通道能否被主动推送（cron 结果 / kernel 注入 / 子代理通知等事件型出站）。

    pull 模型通道（HTTP：出站由调用方自行拉取，``send()`` 为空实现）应置 False。
    发布侧据此如实报告"投递失败"，而不是静默丢弃却判成功。
    """

    # ====== 生命周期 ======
    def on_load(self) -> None:
        """通道插件加载时自注册到内核。"""
        if not self.display_name:
            self.display_name = self.metadata.name
        logger.info("通道 {} ({}) 已加载", self.display_name, self.metadata.plugin_type)

    def on_enable(self) -> None:
        """通道插件启用时，订阅 agent.outbound 事件以接收出站消息。

        事件型出站（**非**正常回复路径——正常回复由 handle_message 返回值直投）
        共三个发布者，全部经 ``nanobee.outbound.publish_outbound``：
        - Cron 插件定时任务结果（``builtin/tool_cron/plugin.py``）
        - 内核结果注入（``kernel/kernel.py``）
        - AgentLoop 子代理启动通知（``agent/loop.py``）

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

        载荷键集合由 :func:`nanobee.outbound.outbound_payload` 单点决定，
        本入口按契约整体透传（含 ``media``）——通道不消费是通道自己的选择，
        入口不得丢字段。

        守卫为「正文与附件至少一个非空」：**纯附件（正文为空）是合法形态**
        （如 cron 周报只产出一个 MD 附件）。

        Args:
            data: 事件数据，包含 channel、chat_id、content、media、metadata
        """
        if not isinstance(data, dict):
            return
        channel_name = data.get("channel", "")
        if channel_name != self.metadata.name:
            return
        chat_id = data.get("chat_id", "direct")
        content = data.get("content") or ""
        media = data.get("media") or []
        if not content and not media:
            return
        msg = OutboundMessage(
            channel=channel_name,
            chat_id=chat_id,
            content=content,
            media=media,
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


__all__ = [
    "ChannelPlugin",
]
