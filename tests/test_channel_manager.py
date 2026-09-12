"""ChannelManager 单元测试 — 通道任务生命周期管理。

覆盖场景：
1. start_channels 启动通道
2. 跳过 safe_for_gateway=False 的通道
3. 通道启动失败不阻塞其他通道
4. shutdown 取消所有任务
5. active_count 计数器
6. _make_error_cb 回调
7. MCP 连接任务的派发与关停对齐（不阻塞启动 / 关停等待 / 异常取回 / 超时留痕）
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.kernel.channel_manager import ChannelManager
from nanobee.utils.logger import logger


@contextmanager
def _captured_errors() -> Iterator[list[str]]:
    """捕获 ERROR 及以上级别的日志正文。"""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="ERROR", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


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
    async def test_connect_mcp_dispatched_and_tracked(self) -> None:
        """connect_mcp 被派发为受管任务：已启动且被持引用（关停时可等待）。"""
        mgr = ChannelManager()
        release = asyncio.Event()
        called = False

        async def _connect() -> None:
            nonlocal called
            called = True
            await release.wait()

        await mgr.start_channels([], connect_mcp=_connect)
        await asyncio.sleep(0)

        assert called
        assert mgr._mcp_task is not None and not mgr._mcp_task.done()

        release.set()
        await mgr.shutdown()

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


class TestChannelManagerMcpDispatch:
    """MCP 连接任务的派发与关停对齐。

    MCP 连接必须由 ChannelManager 受管启动，同时满足两个相反方向的约束：

    1. **不阻塞启动**——MCP 握手是串行且可能长达数十秒的，若在 gateway 的 ready
       路径同步等待，会让 ``nanobee svc start`` 在 10s 健康检查上误判失败；
    2. **关停可对齐**——连接仍在飞行时若直接关闭，``close_mcp()`` 可能看到空
       ``_owners`` 直接返回，随后连接建成却无人回收（连接/子进程/task 泄漏）。
    """

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_in_flight_mcp_connect(self) -> None:
        """关停时必须等在飞行中的 MCP 连接落地，而不是放手不管。"""
        mgr = ChannelManager()
        landed = asyncio.Event()

        async def _connecting() -> None:
            await asyncio.sleep(0.05)
            landed.set()

        await mgr.start_channels([], connect_mcp=_connecting)
        await mgr.shutdown()

        assert landed.is_set()

    @pytest.mark.asyncio
    async def test_mcp_connect_failure_is_logged(self) -> None:
        """MCP 连接任务失败必须被取回并记 ERROR，不能静默消失。"""
        mgr = ChannelManager()

        async def _boom() -> None:
            raise RuntimeError("连接炸了")

        with _captured_errors() as messages:
            await mgr.start_channels([], connect_mcp=_boom)
            await mgr.shutdown()

        assert any("MCP 连接" in message for message in messages)

    @pytest.mark.asyncio
    async def test_start_channels_does_not_block_on_mcp_connect(self) -> None:
        """启动不得等待 MCP 连接完成。

        MCP 握手是串行且可能长达数十秒的：若在 gateway 的 ready 路径同步等待，
        ``nanobee svc start`` 会在 10s 健康检查上误判失败。这条用例是「不得改回
        ``await connect_mcp()``」这个决策的保护栏。
        """
        mgr = ChannelManager()
        finished = False

        async def _slow_connect() -> None:
            nonlocal finished
            await asyncio.sleep(0.2)
            finished = True

        await mgr.start_channels([], connect_mcp=_slow_connect)

        assert not finished, "start_channels 不应等待 MCP 连接完成"

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_task_not_counted_in_active_count(self) -> None:
        """active_count 语义仍是「通道任务数」，不含 MCP 连接任务。"""
        mgr = ChannelManager()
        release = asyncio.Event()

        async def _blocking_connect() -> None:
            await release.wait()

        await mgr.start_channels([], connect_mcp=_blocking_connect)
        await asyncio.sleep(0)

        assert mgr.active_count == 0

        release.set()
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_does_not_cancel_mcp_connect(self) -> None:
        """关停是「等它落地」而不是「取消它」。

        取消只中断 ``connect()`` 内部的 await，各 server 独立的 owner task 仍会
        继续建连——收不到口，只会留下一批无人回收的连接。
        """
        mgr = ChannelManager()
        completed = False
        cancelled = False

        async def _connecting() -> None:
            nonlocal completed, cancelled
            try:
                await asyncio.sleep(0.05)
                completed = True
            except asyncio.CancelledError:
                cancelled = True
                raise

        await mgr.start_channels([], connect_mcp=_connecting)
        await mgr.shutdown()

        assert completed
        assert not cancelled

    @pytest.mark.asyncio
    async def test_wait_background_times_out_with_error_log(self) -> None:
        """等待超上界时必须记 ERROR，且不得把关停无限拖住。

        这里是本项目里少数**允许**用 ``asyncio.wait_for``/``timeout`` 包裹的调用：
        ``wait_background`` 内部只 ``await`` 一个 future/任务，不 enter cancel scope。
        """
        mgr = ChannelManager()
        release = asyncio.Event()

        async def _stuck_connect() -> None:
            await release.wait()

        await mgr.start_channels([], connect_mcp=_stuck_connect)
        await asyncio.sleep(0)

        with patch("nanobee.kernel.channel_manager._MCP_CONNECT_DRAIN_TIMEOUT_S", 0.05):
            with _captured_errors() as messages:
                async with asyncio.timeout(1):
                    await mgr.wait_background()

        assert any("MCP" in message for message in messages), "等待超上界必须记 ERROR"

        release.set()
        await mgr.shutdown()
