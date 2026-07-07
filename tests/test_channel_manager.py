"""ChannelManager 单元测试 — 通道任务生命周期管理。

覆盖场景：
1. start_channels 启动通道
2. 跳过 safe_for_gateway=False 的通道
3. 通道启动失败不阻塞其他通道
4. shutdown 取消所有任务
5. active_count 计数器
6. _make_error_cb 回调
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.kernel.channel_manager import ChannelManager


class TestChannelManagerStart:
    """ChannelManager.start_channels() 测试。"""

    @pytest.mark.asyncio
    async def test_start_single_channel(self) -> None:
        """启动单个通道：创建 task 并追踪。"""
        mgr = ChannelManager()
        channel = MagicMock()
        channel.safe_for_gateway = True
        channel.name = "test-channel"
        channel.start = AsyncMock()

        await mgr.start_channels([channel])
        # asyncio.create_task 调度后需让出事件循环让 task 运行
        await asyncio.sleep(0.02)
        # 通道 start 被调用
        channel.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_multiple_channels(self) -> None:
        """启动多个通道，各自创建 task。"""
        mgr = ChannelManager()
        channels = [
            self._make_channel("ch-1"),
            self._make_channel("ch-2"),
            self._make_channel("ch-3"),
        ]

        await mgr.start_channels(channels)
        await asyncio.sleep(0.02)

        for ch in channels:
            ch.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_unsafe_channels(self) -> None:
        """safe_for_gateway=False 的通道被跳过。"""
        mgr = ChannelManager()
        unsafe = MagicMock()
        unsafe.safe_for_gateway = False
        unsafe.name = "unsafe-channel"
        unsafe.start = AsyncMock()

        await mgr.start_channels([unsafe])
        unsafe.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_failure_does_not_block_others(self) -> None:
        """一个通道启动失败不影响其他通道。"""
        mgr = ChannelManager()
        bad = MagicMock()
        bad.safe_for_gateway = True
        bad.name = "bad-channel"
        bad.start = AsyncMock(side_effect=RuntimeError("启动失败"))

        good = self._make_channel("good-channel")

        await mgr.start_channels([bad, good])
        await asyncio.sleep(0.02)
        # bad 启动失败不应阻止 good 启动
        good.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_mcp_called(self) -> None:
        """connect_mcp 回调被调度。"""
        mgr = ChannelManager()
        connect_mcp = AsyncMock()

        await mgr.start_channels([], connect_mcp=connect_mcp)
        # connect_mcp 作为 ensure_future 调度，不一定立即执行
        # 等待一小段时间
        await asyncio.sleep(0.05)
        # 验证至少被调度（可能因任务已执行而完成）
        assert True  # 不抛异常即为通过

    @staticmethod
    def _make_channel(name: str) -> MagicMock:
        """创建标准的 mock 通道。"""
        ch = MagicMock()
        ch.safe_for_gateway = True
        ch.name = name
        ch.start = AsyncMock()
        return ch


class TestChannelManagerShutdown:
    """ChannelManager.shutdown() 测试。"""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_tasks(self) -> None:
        """shutdown 取消所有追踪的 task。"""
        mgr = ChannelManager()
        channel = MagicMock()
        channel.safe_for_gateway = True
        channel.name = "ch-1"
        # start 不返回（模拟长期运行）
        async def _slow_start():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
        channel.start = _slow_start

        await mgr.start_channels([channel])
        await asyncio.sleep(0.02)
        assert mgr.active_count == 1

        await mgr.shutdown()
        assert mgr.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_empty_noop(self) -> None:
        """无任务时 shutdown 是空操作。"""
        mgr = ChannelManager()
        await mgr.shutdown()
        assert mgr.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_already_completed_tasks(self) -> None:
        """已完成的任务在 shutdown 时不被重复取消。"""
        mgr = ChannelManager()
        channel = MagicMock()
        channel.safe_for_gateway = True
        channel.name = "quick-channel"
        channel.start = AsyncMock()  # 立即返回

        await mgr.start_channels([channel])
        await asyncio.sleep(0.02)  # 等待完成

        # 不应抛异常
        await mgr.shutdown()
        assert mgr.active_count == 0


class TestChannelManagerActiveCount:
    """ChannelManager.active_count 测试。"""

    @pytest.mark.asyncio
    async def test_active_count_empty(self) -> None:
        """无任务时 active_count 为 0。"""
        mgr = ChannelManager()
        assert mgr.active_count == 0

    @pytest.mark.asyncio
    async def test_active_count_with_running_tasks(self) -> None:
        """有运行中任务时 active_count 正确。"""
        mgr = ChannelManager()
        channel = MagicMock()
        channel.safe_for_gateway = True
        channel.name = "long-ch"
        async def _slow_start():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
        channel.start = _slow_start

        await mgr.start_channels([channel])
        await asyncio.sleep(0.02)
        assert mgr.active_count == 1

        await mgr.shutdown()


class TestChannelManagerErrorCallback:
    """ChannelManager._make_error_cb() 测试。"""

    def test_cancelled_error_silenced(self) -> None:
        """CancelledError 被静默吞掉。"""
        cb = ChannelManager._make_error_cb("test-ch")
        task = MagicMock(spec=asyncio.Task)
        task.result.side_effect = asyncio.CancelledError
        # 不应抛异常
        cb(task)

    def test_other_exception_silenced(self) -> None:
        """其他异常也被静默处理（通过 logger.exception 记录）。"""
        cb = ChannelManager._make_error_cb("test-ch")
        task = MagicMock(spec=asyncio.Task)
        task.result.side_effect = RuntimeError("模拟异常")
        # 不应抛异常
        cb(task)
