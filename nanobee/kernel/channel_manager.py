"""通道任务生命周期管理"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobee.utils.logger import logger


class ChannelManager:
    """管理通道插件的 asyncio.Task 生命周期。

    职责单一：启动 / 停止 / 等待通道后台任务。
    不关心通道是什么、怎么工作。
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start_channels(
        self,
        channels: list[Any],
        *,
        connect_mcp: Any = None,  # async callable, optional
    ) -> None:
        """启动所有 safe_for_gateway 的通道插件。"""
        for channel in channels:
            if not getattr(channel, "safe_for_gateway", True):
                logger.info("通道 {} 跳过 Gateway 启动", getattr(channel, "name", "?"))
                continue
            name = getattr(channel, "name", "?")
            try:
                task = asyncio.create_task(channel.start())
                task.add_done_callback(self._make_error_cb(name))
                self._tasks.append(task)
            except Exception:
                logger.exception("通道插件 {} 启动失败，已跳过", name)

        if connect_mcp is not None:
            asyncio.ensure_future(connect_mcp())

    async def shutdown(self) -> None:
        """停止所有通道后台任务：取消 → 等待 3s 超时兜底。"""
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=3, return_when=asyncio.ALL_COMPLETED)
        self._tasks.clear()

    @property
    def active_count(self) -> int:
        """当前活跃（未完成）的通道任务数。"""
        return sum(1 for t in self._tasks if not t.done())

    @staticmethod
    def _make_error_cb(name: str) -> Any:
        def _cb(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("通道 {} 后台任务异常退出", name)
        return _cb
