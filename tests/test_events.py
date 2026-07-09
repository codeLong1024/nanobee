"""事件系统单元测试 — EventBus 与 RuntimeEventBus"""

from __future__ import annotations

import asyncio

import pytest

from nanobee.events.event_bus import EventBus
from nanobee.events.runtime_events import (
    KernelBooted,
    RuntimeEventBus,
    SoulViolationEvent,
)


# =============================================================================
# EventBus — 订阅 / 发布 / 取消订阅
# =============================================================================


class TestEventBusSubscribe:
    """subscribe / publish 基本行为。"""

    @pytest.mark.asyncio
    async def test_sync_handler_called(self) -> None:
        """同步 handler 被正确调用。"""
        bus = EventBus()
        results: list[str] = []

        def handler(data: str) -> None:
            results.append(data)

        bus.subscribe("test.event", handler)
        await bus.publish("test.event", "hello")
        assert results == ["hello"]

    @pytest.mark.asyncio
    async def test_async_handler_awaited(self) -> None:
        """异步 handler 被正确等待。"""
        bus = EventBus()
        results: list[str] = []

        async def handler(data: str) -> None:
            await asyncio.sleep(0)
            results.append(data)

        bus.subscribe("test.event", handler)
        await bus.publish("test.event", "async_hello")
        assert results == ["async_hello"]

    @pytest.mark.asyncio
    async def test_multiple_handlers_all_called(self) -> None:
        """多个 handler 都被调用。"""
        bus = EventBus()
        results: list[str] = []

        bus.subscribe("test.event", lambda d: results.append(f"a:{d}"))
        bus.subscribe("test.event", lambda d: results.append(f"b:{d}"))
        await bus.publish("test.event", "x")
        assert results == ["a:x", "b:x"]

    @pytest.mark.asyncio
    async def test_no_handlers_silent(self) -> None:
        """无 handler 时发布不崩溃。"""
        bus = EventBus()
        await bus.publish("no.subscribers", "data")  # 不抛异常


class TestEventBusSubscribeReturn:
    """subscribe 返回取消函数。"""

    @pytest.mark.asyncio
    async def test_unsubscribe_via_returned_function(self) -> None:
        """返回的取消函数可取消订阅。"""
        bus = EventBus()
        results: list[str] = []

        unsub = bus.subscribe("test.event", lambda d: results.append(d))
        await bus.publish("test.event", "first")
        unsub()
        await bus.publish("test.event", "second")
        assert results == ["first"]

    @pytest.mark.asyncio
    async def test_unsubscribe_twice_safe(self) -> None:
        """重复取消不抛异常。"""
        bus = EventBus()
        unsub = bus.subscribe("test.event", lambda d: None)
        unsub()
        unsub()  # 不应抛异常


class TestEventBusUnsubscribe:
    """unsubscribe 方法鲁棒性。"""

    def test_missing_event_silent(self) -> None:
        """取消不存在的事件不抛异常。"""
        bus = EventBus()

        def handler(x: str) -> None:
            pass

        bus.unsubscribe("nonexistent", handler)  # 不应抛异常

    def test_missing_handler_silent(self) -> None:
        """取消未注册的 handler 不抛异常。"""
        bus = EventBus()

        def handler_a(x: str) -> None:
            pass

        def handler_b(x: str) -> None:
            pass

        bus.subscribe("test.event", handler_a)
        bus.unsubscribe("test.event", handler_b)  # handler_b 不在列表
        # 不应抛异常


class TestEventBusTypeCheck:
    """subscribe 类型校验。"""

    def test_non_callable_raises_typeerror(self) -> None:
        """非 callable handler 抛 TypeError。"""
        bus = EventBus()
        with pytest.raises(TypeError, match="must be callable"):
            bus.subscribe("test.event", "not_a_function")  # type: ignore[arg-type]


class TestEventBusErrorIsolation:
    """处理器异常隔离。"""

    @pytest.mark.asyncio
    async def test_failing_handler_does_not_block_others(self) -> None:
        """一个 handler 抛异常不阻止其他 handler 执行。"""
        bus = EventBus()
        results: list[str] = []

        def failing_handler(data: str) -> None:
            raise RuntimeError("boom")

        bus.subscribe("test.event", failing_handler)
        bus.subscribe("test.event", lambda d: results.append(d))
        await bus.publish("test.event", "ok")
        assert results == ["ok"]


# =============================================================================
# RuntimeEventBus — 类型化事件订阅 / 发布 / 取消订阅
# =============================================================================


class TestRuntimeEventBusSubscribe:
    """subscribe / publish 基本行为。"""

    @pytest.mark.asyncio
    async def test_handler_called_for_matching_type(self) -> None:
        """匹配的类型调用 handler。"""
        bus = RuntimeEventBus()
        events: list[SoulViolationEvent] = []

        def handler(e: SoulViolationEvent) -> None:
            events.append(e)

        bus.subscribe(handler, SoulViolationEvent)
        evt = SoulViolationEvent(path="/tmp/x", content_preview="abc")
        await bus.publish(evt)
        assert len(events) == 1
        assert events[0].path == "/tmp/x"

    @pytest.mark.asyncio
    async def test_handler_not_called_for_wrong_type(self) -> None:
        """不匹配的类型不调用 handler。"""
        bus = RuntimeEventBus()
        events: list[SoulViolationEvent] = []

        bus.subscribe(lambda e: events.append(e), SoulViolationEvent)  # type: ignore[arg-type]
        await bus.publish(KernelBooted())
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_catch_all_handler_called_for_any_type(self) -> None:
        """event_type=None 接收所有类型事件。"""
        bus = RuntimeEventBus()
        events: list[object] = []

        bus.subscribe(lambda e: events.append(e))
        await bus.publish(KernelBooted())
        await bus.publish(SoulViolationEvent(path="/x", content_preview=""))
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_async_handler_awaited(self) -> None:
        """异步 handler 被正确等待。"""
        bus = RuntimeEventBus()
        results: list[str] = []

        async def handler(e: KernelBooted) -> None:
            await asyncio.sleep(0)
            results.append("done")

        bus.subscribe(handler, KernelBooted)
        await bus.publish(KernelBooted())
        assert results == ["done"]


class TestRuntimeEventBusUnsubscribe:
    """取消订阅。"""

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_calling(self) -> None:
        """取消后不再调用 handler。"""
        bus = RuntimeEventBus()
        calls: list[str] = []

        unsub = bus.subscribe(lambda e: calls.append("x"), KernelBooted)
        await bus.publish(KernelBooted())
        unsub()
        await bus.publish(KernelBooted())
        assert calls == ["x"]

    def test_unsubscribe_twice_safe(self) -> None:
        """重复取消不抛异常。"""
        bus = RuntimeEventBus()
        unsub = bus.subscribe(lambda e: None, KernelBooted)
        unsub()
        unsub()  # 安全


class TestRuntimeEventBusTypeCheck:
    """subscribe 类型校验。"""

    def test_non_callable_raises_typeerror(self) -> None:
        """非 callable 抛 TypeError。"""
        bus = RuntimeEventBus()
        with pytest.raises(TypeError, match="must be callable"):
            bus.subscribe("not_callable")  # type: ignore[arg-type]


class TestRuntimeEventBusErrorIsolation:
    """处理器异常隔离。"""

    @pytest.mark.asyncio
    async def test_failing_handler_does_not_block_others(self) -> None:
        """一个 handler 抛异常不阻止其他 handler 执行。"""
        bus = RuntimeEventBus()
        results: list[str] = []

        def failing(e: KernelBooted) -> None:
            raise RuntimeError("boom")

        bus.subscribe(failing, KernelBooted)
        bus.subscribe(lambda e: results.append("ok"), KernelBooted)
        await bus.publish(KernelBooted())
        assert results == ["ok"]


class TestRuntimeEventBusHandlerCount:
    """handler_count 属性。"""

    def test_initial_zero(self) -> None:
        bus = RuntimeEventBus()
        assert bus.handler_count == 0

    def test_reflects_subscriptions(self) -> None:
        bus = RuntimeEventBus()
        bus.subscribe(lambda e: None, KernelBooted)
        bus.subscribe(lambda e: None, SoulViolationEvent)
        assert bus.handler_count == 2

    def test_reflects_unsubscribe(self) -> None:
        bus = RuntimeEventBus()
        unsub = bus.subscribe(lambda e: None, KernelBooted)
        assert bus.handler_count == 1
        unsub()
        assert bus.handler_count == 0


# =============================================================================
# 事件类型 dataclass
# =============================================================================


class TestSoulViolationEvent:
    """SoulViolationEvent 数据类。"""

    def test_frozen(self) -> None:
        evt = SoulViolationEvent(path="/tmp/core.md", content_preview="bad stuff")
        with pytest.raises(Exception):  # FrozenInstanceError
            evt.path = "/other"  # type: ignore[misc]

    def test_attributes(self) -> None:
        evt = SoulViolationEvent(path="/p", content_preview="cp")
        assert evt.path == "/p"
        assert evt.content_preview == "cp"


class TestKernelBooted:
    """KernelBooted 数据类。"""

    def test_can_create(self) -> None:
        evt = KernelBooted()
        assert isinstance(evt, KernelBooted)


# =============================================================================
# 聚合导出
# =============================================================================


def test_events_package_exports() -> None:
    """events 包公共 API 可导入。"""
    from nanobee.events import (  # type: ignore[attr-defined]
        EventBus,
        KernelBooted,
        RuntimeEventBus,
        SoulViolationEvent,
    )
    assert EventBus is not None
    assert RuntimeEventBus is not None
    assert SoulViolationEvent is not None
    assert KernelBooted is not None
