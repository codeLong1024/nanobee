"""
运行时事件总线 — 类型安全的内核内部状态通知

与 EventBus 的分工：
- EventBus：字符串 key 的发布/订阅，供插件使用（已存在）
- RuntimeEventBus：类型化事件，用于内核内部状态通知

设计参考 nanobot/bus/runtime_events.py，但更轻量。
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nanobee.utils.logger import logger



# =============================================================================
# 事件类型定义
# =============================================================================


@dataclass(frozen=True)
class SoulViolation:
    """灵魂文件写入拦截事件。"""
    path: str
    content_preview: str


@dataclass(frozen=True)
class KernelBooted:
    """内核启动完成事件。"""
    pass


# 运行时事件联合类型
RuntimeEvent = SoulViolation | KernelBooted
RuntimeEventType = (
    type[SoulViolation]
    | type[KernelBooted]
)

RuntimeEventHandler = Callable[[RuntimeEvent], Awaitable[None] | None]


# =============================================================================
# 运行时事件总线
# =============================================================================


class RuntimeEventBus:
    """类型安全的内核运行时事件总线。

    subscribe 支持按事件类型过滤，publish 按类型分发给匹配的订阅者。
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[RuntimeEventType | None, RuntimeEventHandler]] = []

    def subscribe(
        self,
        handler: RuntimeEventHandler,
        event_type: RuntimeEventType | None = None,
    ) -> Callable[[], None]:
        """订阅事件。

        Args:
            handler: 事件处理器，接收 RuntimeEvent 实例
            event_type: 可选的事件类型过滤。为 None 时接收所有事件。

        Returns:
            取消订阅的函数。
        """
        entry = (event_type, handler)
        self._handlers.append(entry)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return _unsubscribe

    async def publish(self, event: RuntimeEvent) -> None:
        """发布事件给匹配的订阅者。

        同步和异步 handler 均支持。
        """
        for event_type, handler in list(self._handlers):
            if event_type is not None and not isinstance(event, event_type):
                continue
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "运行时事件处理器 %s 处理 %s 出错",
                    handler, type(event).__name__,
                )

    @property
    def handler_count(self) -> int:
        """返回当前注册的处理器数量。"""
        return len(self._handlers)


__all__ = [
    "RuntimeEventBus",
    "RuntimeEvent",
    "SoulViolation",
    "KernelBooted",
]
