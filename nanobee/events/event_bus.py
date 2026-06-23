"""
事件总线 - 事件发布/订阅机制
"""

from __future__ import annotations

from typing import Any, Callable

from nanobee.utils.logger import logger



class EventBus:
    """事件总线"""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}

    def subscribe(self, event: str, handler: Callable) -> None:
        """订阅事件

        Args:
            event: 事件名称
            handler: 事件处理器

        Raises:
            TypeError: 当 handler 不是可调用对象时
        """
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {type(handler).__name__}")
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(handler)

    async def publish(self, event: str, data: Any = None) -> None:
        """发布事件

        Args:
            event: 事件名称
            data: 事件数据
        """
        handlers = self._subscribers.get(event, [])
        if not handlers:
            return
        # 快照复制，防止迭代过程中订阅列表被修改
        for handler in list(handlers):
            try:
                result = handler(data)
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.exception("事件处理器 {} 处理事件 {} 出错", handler, event)

    def unsubscribe(self, event: str, handler: Callable) -> None:
        """取消订阅

        Args:
            event: 事件名称
            handler: 事件处理器
        """
        if event in self._subscribers:
            self._subscribers[event].remove(handler)
