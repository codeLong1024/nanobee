"""
锁管理器 - 按用户粒度管理并发

保证：
- 同一 user_id 的任务串行执行
- 不同 user_id 的任务可并行执行
- 支持 async with 上下文管理器
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class LockManager:
    """锁管理器 - 按用户粒度管理并发

    封装 asyncio.Lock 字典，提供按 user_id 粒度的互斥锁。
    同一 user_id 串行，不同 user_id 并行。
    """

    def __init__(self, max_concurrent: int = 0):
        """初始化锁管理器

        Args:
            max_concurrent: 全局最大并发数，<=0 表示不限制
        """
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
        )

    @asynccontextmanager
    async def acquire(self, user_id: str) -> AsyncIterator[None]:
        """获取指定用户的锁

        同一 user_id 串行，不同 user_id 可并行。
        支持嵌套调用（同一协程内可重入）。

        Args:
            user_id: 用户标识

        Yields:
            锁释放
        """
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        gate = self._concurrency_gate or _null_context()

        async with lock, gate:
            yield

    def current_locks(self) -> list[str]:
        """返回当前持有锁的用户列表（用于调试/监控）"""
        return [uid for uid, lk in self._user_locks.items() if lk.locked()]

    @property
    def active_users(self) -> int:
        """当前活跃用户数"""
        return len(self._user_locks)

    def clear(self) -> None:
        """清空所有锁（在 shutdown 时调用）"""
        self._user_locks.clear()
        logger.debug("LockManager 已清空")


class _NullContextManager:
    """空的异步上下文管理器，当不限制并发时使用"""

    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args: object) -> None:
        pass


_null_context = _NullContextManager

__all__ = [
    "LockManager",
]
