"""消息分发器 — 管理消息消费循环和串行分发。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from nanobee.agent.messages import InboundMessage, OutboundMessage
from nanobee.kernel.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.router import ContextRouter, UnknownRouteError
from nanobee.utils.logger import logger


class MessageDispatcher:
    """消息分发器。

    管理消息消费循环（run()）和同用户串行分发（dispatch()）。
    不持有 AgentLoop 的状态机，通过回调与之协作。

    职责：
    - 消息消费循环（异步迭代入站消息）
    - 同用户串行分发（LockManager 互斥）
    - 出站消息发布（EventBus）
    - 路由解析（ContextRouter）
    - 待处理消息队列管理（中轮注入）
    - 流式回调桥接
    """

    def __init__(
        self,
        lock_manager: LockManager,
        event_bus: EventBus | None,
        router: ContextRouter,
        process_message_cb: Callable[..., Awaitable[OutboundMessage | None]],
    ) -> None:
        """初始化消息分发器。

        Args:
            lock_manager: 用户级互斥锁管理器
            event_bus: 事件总线（用于发布出站和流式事件）
            router: 上下文路由器
            process_message_cb: AgentLoop._process_message 的绑定方法引用
        """
        self._lock_manager = lock_manager
        self._event_bus = event_bus
        self._router = router
        self._process_message = process_message_cb
        self._pending_queues: dict[str, asyncio.Queue] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._running = False

    @property
    def pending_queues(self) -> dict[str, asyncio.Queue]:
        """获取待处理消息队列字典（供 Kernel 外部访问）。"""
        return self._pending_queues

    # ── 消息消费循环 ──────────────────────────────────────────────────

    async def run(
        self,
        consume_fn: Callable[[], Awaitable[InboundMessage]],
        connect_mcp_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """启动消息消费循环。

        Args:
            consume_fn: 消费入站消息的异步函数
            connect_mcp_fn: 连接 MCP 服务器的异步函数
        """
        self._running = True
        await connect_mcp_fn()
        logger.info("Agent loop 已启动")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    consume_fn(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                if not self._running:
                    raise
                continue
            except Exception as e:
                logger.warning("消费入站消息出错: {error}, 继续处理...", error=e)
                continue

            effective_key = self._effective_context_id(msg)
            # 如果该上下文已有活跃的待处理队列，路由到那里
            if effective_key in self._pending_queues:
                try:
                    self._pending_queues[effective_key].put_nowait(msg)
                except asyncio.QueueFull:
                    logger.warning("上下文 {ctx_id} 待处理队列已满，回退为排队任务", ctx_id=effective_key)
                else:
                    logger.info("后续消息已路由到上下文 {ctx_id} 的待处理队列", ctx_id=effective_key)
                    continue

            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    def stop(self) -> None:
        """停止消息消费循环。"""
        self._running = False

    # ── 单消息分发 ────────────────────────────────────────────────────

    async def _dispatch(self, msg: InboundMessage) -> None:
        """处理消息：同用户串行，跨用户并行。

        使用 msg.sender_id 作为 context_id (用户唯一标识),
        参考 nanobot_channel_dingtalk 的设计,避免创建重复目录。
        """
        # 优先使用 sender_id 作为 context_id
        context_id = msg.sender_id or self._effective_context_id(msg)

        # 注册待处理队列
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[context_id] = pending

        try:
            async with self._lock_manager.acquire(context_id):
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        stream_base_id = f"{msg.context_id}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream_fn(delta: str) -> None:
                            if self._event_bus:
                                await self._event_bus.publish("agent.stream_delta", {
                                    "context_id": context_id,
                                    "stream_id": _current_stream_id(),
                                    "delta": delta,
                                })

                        async def on_stream_end_fn(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            if self._event_bus:
                                await self._event_bus.publish("agent.stream_end", {
                                    "context_id": context_id,
                                    "stream_id": _current_stream_id(),
                                    "resuming": resuming,
                                })
                            stream_segment += 1

                        on_stream = on_stream_fn
                        on_stream_end = on_stream_end_fn

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self._publish_outbound(response)
                    else:
                        logger.debug("消息处理返回 None，不发送响应")
                except asyncio.CancelledError:
                    logger.info("上下文 {ctx_id} 的任务被取消", ctx_id=context_id)
                    raise
                except Exception:
                    logger.exception("处理上下文 {ctx_id} 的消息出错", ctx_id=context_id)
                    await self._publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
        finally:
            # 排空待处理队列，重新发布为独立入站消息
            queue = self._pending_queues.pop(context_id, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    leftover += 1
                if leftover:
                    logger.info("上下文 {ctx_id} 有 {left} 条剩余消息被丢弃", ctx_id=context_id, left=leftover)

    async def _publish_outbound(self, msg: OutboundMessage) -> None:
        """发布出站消息（由通道插件调用）。"""
        if self._event_bus:
            await self._event_bus.publish("agent.outbound", {
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content": msg.content,
                "metadata": msg.metadata,
            })

    # ── 路由辅助 ──────────────────────────────────────────────────────

    def _effective_context_id(self, msg: InboundMessage) -> str:
        """返回用于任务路由和中轮注入的上下文 ID（即 user_id）。

        路由优先级：
        1. msg.context_id_override 显式指定
        2. 路由器根据 channel:chat_id 查找
        3. 未知路由直接使用 msg.context_id
        """
        try:
            return self._router.resolve(
                msg.channel, msg.chat_id,
                override=msg.context_id_override,
            )
        except UnknownRouteError:
            # 保持向后兼容：如果没有路由器配置，降级使用 msg.context_id
            return msg.context_id