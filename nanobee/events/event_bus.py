"""
事件总线 — 事件发布/订阅机制

- EventBus：字符串 key 的发布/订阅，供插件使用
- RuntimeEventBus：类型化事件，用于内核内部状态通知（见 runtime_events.py）

处理器异常相互隔离：单个处理器失败不影响其他处理器，发布者不感知。
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Any, Callable

from nanobee.utils.logger import logger


EventHandler = Callable[[Any], Any]


class EventBus:
    """事件总线 — 基于字符串 key 的发布/订阅。

    subscribe 支持同步和异步 handler，publish 按 event 名分发。
    处理器异常相互隔离，不中断其他订阅者。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event: str, handler: EventHandler) -> Callable[[], None]:
        """订阅事件，返回取消订阅的函数。

        Args:
            event: 事件名称
            handler: 事件处理器

        Raises:
            TypeError: 当 handler 不是可调用对象时

        Returns:
            取消订阅的零参数函数。
        """
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {type(handler).__name__}")
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(handler)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.get(event, []).remove(handler)

        return _unsubscribe

    async def publish(self, event: str, data: Any = None) -> None:
        """发布事件给所有匹配的订阅者。

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
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("事件处理器 {} 处理事件 {} 出错", handler, event)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        """取消订阅。事件或处理器不存在时静默忽略。

        Args:
            event: 事件名称
            handler: 事件处理器
        """
        with contextlib.suppress(ValueError):
            self._subscribers.get(event, []).remove(handler)

__all__ = [
    "EventBus",
    "EventHandler",
]
