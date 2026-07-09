"""
事件系统 — 双总线发布/订阅机制

- EventBus：字符串 key 的发布/订阅，供插件使用
- RuntimeEventBus：类型化事件，用于内核内部状态通知
"""
from __future__ import annotations

from nanobee.events.event_bus import EventBus, EventHandler
from nanobee.events.runtime_events import (
    KernelBooted,
    RuntimeEvent,
    RuntimeEventBus,
    RuntimeEventHandler,
    RuntimeEventType,
    SoulViolationEvent,
)

__all__ = [
    "EventBus",
    "EventHandler",
    "KernelBooted",
    "RuntimeEvent",
    "RuntimeEventBus",
    "RuntimeEventHandler",
    "RuntimeEventType",
    "SoulViolationEvent",
]
