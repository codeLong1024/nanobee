"""测试 MessageDispatcher — 消息分发器。"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from nanobee.agent.dispatcher import MessageDispatcher
from nanobee.agent.messages import InboundMessage, OutboundMessage
from nanobee.kernel.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.router import ContextRouter


@pytest.fixture
def lock_manager():
    return LockManager(max_concurrent=3)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def router():
    return ContextRouter()


@pytest.fixture
def process_message():
    """返回一个记录调用并返回 OutboundMessage 的 mock。"""
    async def _process(msg, **kwargs):
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"response to: {msg.content[:50]}",
        )
    return _process


@pytest.fixture
def dispatcher(lock_manager, event_bus, router, process_message):
    return MessageDispatcher(
        lock_manager=lock_manager,
        event_bus=event_bus,
        router=router,
        process_message_cb=process_message,
    )


class TestMessageDispatcherInit:
    """MessageDispatcher 初始化测试。"""

    def test_init(self, dispatcher):
        """正常初始化。"""
        assert dispatcher._pending_queues == {}
        assert dispatcher._active_tasks == {}
        assert not dispatcher._running

    def test_pending_queues_property(self, dispatcher):
        """pending_queues 属性返回内部字典。"""
        assert dispatcher.pending_queues is dispatcher._pending_queues


class TestMessageDispatcherStop:
    """MessageDispatcher.stop() 测试。"""

    def test_stop_sets_running_false(self, dispatcher):
        """stop() 将 _running 设为 False。"""
        dispatcher._running = True
        dispatcher.stop()
        assert not dispatcher._running

    def test_stop_idempotent(self, dispatcher):
        """多次调用 stop() 安全。"""
        dispatcher.stop()
        dispatcher.stop()
        assert not dispatcher._running


class TestMessageDispatcherDispatch:
    """MessageDispatcher._dispatch() 测试。"""

    @pytest.mark.asyncio
    async def test_dispatch_creates_queue(self, dispatcher, event_bus):
        """分发消息时创建并清除待处理队列。"""
        msg = InboundMessage(
            channel="test", sender_id="user1", chat_id="chat1", content="hello",
        )
        outbound_spy = AsyncMock()
        event_bus.subscribe("agent.outbound", outbound_spy)

        await dispatcher._dispatch(msg)

        assert "user1" not in dispatcher._pending_queues  # 处理后清除
        outbound_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_propagates_cancelled(self, dispatcher):
        """分发时抛出 CancelledError 向上传播。"""
        async def failing_process(msg, **kwargs):
            raise asyncio.CancelledError()

        dispatcher._process_message = failing_process
        msg = InboundMessage(
            channel="test", sender_id="user2", chat_id="chat2", content="cancel",
        )
        with pytest.raises(asyncio.CancelledError):
            await dispatcher._dispatch(msg)

    @pytest.mark.asyncio
    async def test_dispatch_sends_error_on_exception(self, dispatcher, event_bus):
        """分发时异常时发送错误出站消息。"""
        async def failing_process(msg, **kwargs):
            raise RuntimeError("boom")

        dispatcher._process_message = failing_process
        published = []

        async def outbound_spy(data):
            published.append(data)

        event_bus.subscribe("agent.outbound", outbound_spy)

        msg = InboundMessage(
            channel="test", sender_id="user3", chat_id="chat3", content="error",
        )
        await dispatcher._dispatch(msg)

        assert len(published) == 1
        content = published[0].get("content", "")
        assert "sorry" in content.lower() or "error" in content.lower()

    @pytest.mark.asyncio
    async def test_dispatch_with_stream_metadata(self, dispatcher, event_bus):
        """流式元数据不会导致错误。"""
        stream_events = []

        async def stream_spy(data):
            stream_events.append(data)

        event_bus.subscribe("agent.stream_delta", stream_spy)

        msg = InboundMessage(
            channel="test", sender_id="user4", chat_id="chat4", content="stream",
            metadata={"_wants_stream": True},
        )
        await dispatcher._dispatch(msg)
        # process_message 无流式输出，stream_delta 不会被调用
        assert len(stream_events) == 0


class TestMessageDispatcherPublish:
    """MessageDispatcher._publish_outbound() 测试。"""

    @pytest.mark.asyncio
    async def test_publishes_to_event_bus(self, dispatcher, event_bus):
        """发布出站消息到事件总线。"""
        published = []

        async def spy(data):
            published.append(data)

        event_bus.subscribe("agent.outbound", spy)

        msg = OutboundMessage(channel="test", chat_id="chat1", content="hi")
        await dispatcher._publish_outbound(msg)

        assert len(published) == 1
        assert published[0]["content"] == "hi"
        assert published[0]["channel"] == "test"

    @pytest.mark.asyncio
    async def test_no_event_bus_skip(self):
        """没有事件总线时跳过发布。"""
        d = MessageDispatcher(
            lock_manager=LockManager(max_concurrent=3),
            event_bus=None,
            router=ContextRouter(),
            process_message_cb=AsyncMock(),
        )
        msg = OutboundMessage(channel="test", chat_id="chat1", content="hi")
        await d._publish_outbound(msg)  # 不应抛出异常


class TestMessageDispatcherRun:
    """MessageDispatcher.run() 测试。"""

    @pytest.mark.asyncio
    async def test_consume_fn_receives_messages(self):
        """消息消费函数被正确调用，stop 后退出循环。"""
        d = MessageDispatcher(
            lock_manager=LockManager(max_concurrent=3),
            event_bus=None,
            router=ContextRouter(),
            process_message_cb=AsyncMock(return_value=None),
        )

        call_count = 0

        async def consume():
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise asyncio.TimeoutError()
            return InboundMessage(
                channel="test", sender_id="u1", chat_id="c1", content="msg",
            )

        async def connect_mcp():
            pass

        # 在后台任务中运行，1 秒后强制停止
        run_task = asyncio.create_task(d.run(consume, connect_mcp))
        await asyncio.sleep(0.1)
        d.stop()
        await asyncio.wait_for(run_task, timeout=2.0)
        assert call_count >= 1
        assert not d._running


class TestMessageDispatcherEffectiveContextId:
    """_effective_context_id 路由测试。"""

    def test_uses_sender_id(self, dispatcher):
        """msg.sender_id 被正确使用。"""
        msg = InboundMessage(
            channel="test", sender_id="user1", chat_id="chat1", content="hi",
        )
        result = dispatcher._effective_context_id(msg)
        assert result == "user1"

    def test_fallback_to_context_id(self, dispatcher):
        """sender_id 为空时使用 msg.context_id。"""
        msg = InboundMessage(
            channel="test", sender_id="", chat_id="chat1", content="hi",
        )
        result = dispatcher._effective_context_id(msg)
        assert result == "test:chat1"

    def test_context_id_override_wins(self, dispatcher):
        """context_id_override 优先。"""
        msg = InboundMessage(
            channel="test", sender_id="user1", chat_id="chat1",
            content="hi", context_id_override="override123",
        )
        result = dispatcher._effective_context_id(msg)
        assert result == "override123"
