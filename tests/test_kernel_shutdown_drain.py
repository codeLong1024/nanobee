"""kernel.shutdown 在途 turn 有界排空测试（Phase 2 终态保证）。

覆盖：
1. 在途 turn 在上界内跑完 → 正常收尾，不取消；
2. 超过上界 → 取消（经 loop 终态保证落 ABANDONED，本文件只验证取消发生）；
3. 排空顺序：turn 排空 → Hook 任务 drain → 通道停止 → MCP 关闭 → 卸载；
4. 关停闸门（评审 #3）：_closing 置位后新消息被拒收并返回关停通知。
"""

from __future__ import annotations

import asyncio

import pytest

from nanobee.config.schema import Config, ShutdownConfig
from nanobee.kernel.kernel import NanobeeKernel


class _FakeLoop:
    """最小 AgentLoop 桩：记录 shutdown 链路调用顺序。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(self) -> None:
        self._events.append("loop.stop")

    async def drain_hook_tasks(self, timeout_s: float) -> int:
        self._events.append(f"drain_hook_tasks({timeout_s})")
        return 0

    async def close_mcp(self) -> None:
        self._events.append("close_mcp")


class _FakePluginManager:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def get_by_type(self, plugin_type: str) -> list:
        return []

    def unload_all(self) -> None:
        self._events.append("unload_all")


class _FakeChannelManager:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def shutdown(self) -> None:
        self._events.append("channel_manager.shutdown")


class _FakeSessionManager:
    def flush_all(self) -> None:
        pass


def _make_kernel(
    events: list[str],
    active_turns: dict[str, asyncio.Task],
    shutdown_cfg: ShutdownConfig | None = None,
) -> NanobeeKernel:
    """绕过 __init__ 构造仅含 shutdown 依赖的最小内核。"""
    k = object.__new__(NanobeeKernel)
    k._active_turns = active_turns
    k.config = Config(shutdown=shutdown_cfg or ShutdownConfig())
    k._agent_loop = _FakeLoop(events)
    k.plugin_manager = _FakePluginManager(events)
    k.channel_manager = _FakeChannelManager(events)
    k.session_manager = _FakeSessionManager()
    k._booted = True
    k._services_started = True
    return k


@pytest.mark.asyncio
async def test_inflight_turn_finishing_within_bound_not_cancelled():
    """在途 turn 在上界内跑完 → 正常收尾，不被取消。"""
    task_done = asyncio.Event()

    async def _quick_turn() -> str:
        await asyncio.sleep(0.01)
        task_done.set()
        return "done"

    task = asyncio.create_task(_quick_turn())
    kernel = _make_kernel(
        [], {"ctx-1": task}, shutdown_cfg=ShutdownConfig(drain_inflight_s=1.0),
    )
    await kernel.shutdown()

    assert task.done() and not task.cancelled()
    assert task.result() == "done"
    assert task_done.is_set()


@pytest.mark.asyncio
async def test_inflight_turn_exceeding_bound_gets_cancelled():
    """超时 turn 被取消（ABANDONED 落账由 loop 终态保证负责）。"""
    task = asyncio.create_task(asyncio.sleep(5.0))
    kernel = _make_kernel(
        [], {"ctx-1": task},
        shutdown_cfg=ShutdownConfig(drain_inflight_s=0.05, drain_cancelled_s=0.1),
    )
    await kernel.shutdown()

    assert task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_shutdown_drain_order():
    """排空顺序：turn 排空 → Hook drain → 通道 → MCP → 卸载。"""
    events: list[str] = []
    kernel = _make_kernel(events, {})

    # 记录 turn 排空发生的时间点（借 _drain_active_turns 的入口日志序）
    original_drain = kernel._drain_active_turns

    async def _spy_drain() -> None:
        events.append("drain_active_turns")
        await original_drain()

    kernel._drain_active_turns = _spy_drain
    await kernel.shutdown()

    assert events.index("drain_active_turns") < events.index("drain_hook_tasks(5.0)")
    assert events.index("drain_hook_tasks(5.0)") < events.index("channel_manager.shutdown")
    assert events.index("channel_manager.shutdown") < events.index("close_mcp")
    assert events.index("close_mcp") < events.index("unload_all")
    # 关停闸门必须在排空前置位（评审 #3）
    assert kernel._closing is True


@pytest.mark.asyncio
async def test_closing_kernel_rejects_new_message_with_notification():
    """关停闸门：_closing 后 handle_message 拒收并返回关停系统通知。"""
    kernel = _make_kernel([], {})
    kernel._closing = True

    response = await kernel.handle_message("你好", context_id="u1")

    # fail-visible：拒收必须携带可渲染的系统通知，而非静默返回 None
    assert response is not None
    assert response.metadata.get("notification_type") == "system"
    assert response.metadata.get("notification_kind") == "kernel_shutting_down"
    assert response.metadata.get("severity") == "warning"
    assert "关停" in response.content


@pytest.mark.asyncio
async def test_shutdown_sets_closing_gate_before_drain():
    """shutdown 先置位闸门再排空：置位后进入的排空阶段无新 turn 竞争。"""
    events: list[str] = []
    kernel = _make_kernel(events, {})
    assert kernel._closing is False

    observed: dict[str, bool] = {}
    original_drain = kernel._drain_active_turns

    async def _spy_drain() -> None:
        observed["closing_at_drain"] = kernel._closing
        await original_drain()

    kernel._drain_active_turns = _spy_drain
    await kernel.shutdown()

    assert observed["closing_at_drain"] is True
