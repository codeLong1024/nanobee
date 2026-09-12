"""通道任务生命周期管理"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobee.agent.mcp_manager import CONNECT_ATTEMPT_TIMEOUT_S, OP_WAIT_SLACK_S
from nanobee.utils.logger import logger

# 关停前等待 MCP 连接任务落地的上限（机制层兜底，非用户可见策略）。
#
# 为什么需要它：MCP 连接是「不阻塞启动」的后台任务，若实例启动后立刻停止，
# 关闭流程可能追上一个仍在建立中的连接——此时 MCPManager 尚未登记任何 owner，
# close() 会直接返回，连接建成后便再无调用方回收（连接/子进程/task 三重泄漏）。
#
# 为什么是「连接预算 + 余量」而不是 5s 这类小值：预算必须覆盖它约束的工作量。
# 该任务内部是各 server 的 _open()（上界 CONNECT_ATTEMPT_TIMEOUT_S，含子进程
# 冷启动），给小值会让慢启动 server（npx）在关停时必然假超时、留下误导性
# ERROR。稳态下该任务早已完成，等待零开销；真正需要等待时，标准做法是
# 「等它落地再关闭」而不是「取消」——取消只中断 connect() 内部的 await，并不
# 会终止各 server 独立的 owner task，收不到口。超时后仍继续关停（owner 已全部
# 登记，close() 的逐 owner 收口依然生效），只是记 ERROR 留痕。
_MCP_CONNECT_DRAIN_TIMEOUT_S = CONNECT_ATTEMPT_TIMEOUT_S + OP_WAIT_SLACK_S


class ChannelManager:
    """管理通道插件与 MCP 连接后台任务的 asyncio.Task 生命周期。

    职责：启动 / 停止通道任务；托管「不阻塞启动、但关停时可等待」的 MCP 连接
    后台任务（持引用 + 异常取回 + 关停前有界等待）。对 MCP 怎么连、连多久一
    无所知——只持有 task 引用，等待预算自 mcp_manager 的机制常量推导。
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        # MCP 连接任务单独持有：它是「不阻塞启动」的后台任务，但必须在关停时
        # 可被等待，因此不能像原来那样 ensure_future 发射即忘。
        self._mcp_task: asyncio.Task | None = None

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
            # 必须持引用 + 挂 done_callback：create_task 出来的匿名 task 一旦抛异常
            # 就是「Task exception was never retrieved」——没人取回、没人记录，
            # 连接失败会彻底静默（原 ensure_future 版本的三大症状之一）。
            task = asyncio.create_task(connect_mcp())
            task.add_done_callback(self._make_error_cb("连接", label="MCP"))
            self._mcp_task = task

    async def wait_background(self) -> None:
        """有界等待 MCP 连接任务落地（**不取消**）。

        不取消的理由：``MCPManager.connect()`` 内部是 ``gather(owner.start())``，
        取消只中断 ``await``，各 server 独立的 owner task 仍会继续建连，等于把
        连接变成无人回收的悬空任务。等待才是收口。
        """
        task = self._mcp_task
        if task is None or task.done():
            return
        _, pending = await asyncio.wait({task}, timeout=_MCP_CONNECT_DRAIN_TIMEOUT_S)
        if pending:
            # 上界触发必须留痕：超时静默正是本类故障长期隐藏的原因。
            logger.error(
                "MCP 连接任务在 {timeout}s 内未落地，继续关停："
                "该连接可能仍在建立中，其 owner task 由 close_mcp() 逐个收口",
                timeout=_MCP_CONNECT_DRAIN_TIMEOUT_S,
            )

    async def shutdown(self) -> None:
        """停止所有通道后台任务：取消 → 等待 3s 超时兜底。

        随后兜底等待 MCP 连接任务落地（本类是该后台任务唯一的等待点，调用方
        不必再自行 wait_background），避免「连接仍在飞行 → 无人登记 → 建成后
        无人回收」的关停窗口。
        """
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=3, return_when=asyncio.ALL_COMPLETED)
        self._tasks.clear()
        await self.wait_background()

    @property
    def active_count(self) -> int:
        """当前活跃（未完成）的通道任务数。"""
        return sum(1 for t in self._tasks if not t.done())

    @staticmethod
    def _make_error_cb(name: str, *, label: str = "通道") -> Any:
        """构造任务异常取回回调：把后台任务的异常记进日志，而不是静默丢弃。

        Args:
            name: 任务名（通道名 / 任务用途），用于定位。
            label: 任务类别前缀，默认「通道」。
        """
        def _cb(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("{} {} 后台任务异常退出", label, name)
        return _cb
