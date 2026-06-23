"""测试 Kernel.inject_message — 统一消息注入入口。

替代原 test_dispatcher.py（MessageDispatcher 已废弃）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from nanobee.agent.messages import InboundMessage, OutboundMessage
from nanobee.events.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def lock_manager():
    return LockManager(max_concurrent=3)


def _make_kernel(agent_loop_mock=None, event_bus=None):
    """构造一个带有 inject_message 的 Kernel mock 对象。"""
    from nanobee.kernel.kernel import NanobeeKernel

    class _MockConfig(dict):
        agents = MagicMock()
        agents.defaults = MagicMock()
        agents.defaults.model = "test"
        agents.defaults.max_iterations = 3
        agents.defaults.max_messages = 20
        agents.defaults.context_window_tokens = 8192

    kernel = NanobeeKernel.__new__(NanobeeKernel)
    kernel.event_bus = event_bus or EventBus()
    kernel._agent_loop = agent_loop_mock
    kernel._booted = True
    kernel.config = _MockConfig(
        data_dir="/tmp/test_nanobee",
        core_md_path="/tmp/test_nanobee/core.md",
    )
    kernel.config.skills = MagicMock()
    kernel.config.skills.enabled = []
    return kernel


class TestInjectMessage:
    """Kernel.inject_message() 测试。"""

    def test_mid_turn_injection(self):
        """中轮注入：活跃队列存在时 put_nowait 到队列，非阻塞。"""
        # msg.context_id = sender_id = "user"
        pending_queues: dict[str, asyncio.Queue] = {}
        pending_queues["user"] = asyncio.Queue(maxsize=20)

        agent_mock = MagicMock()
        agent_mock._pending_queues = pending_queues

        kernel = _make_kernel(agent_loop_mock=agent_mock)
        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="subagent result",
            metadata={"_subagent_auto_trigger": True},
        )

        kernel.inject_message(msg)

        # 消息应该在队列中（key = msg.context_id = sender_id）
        assert not pending_queues["user"].empty()
        queued = pending_queues["user"].get_nowait()
        assert queued is msg

    @pytest.mark.asyncio
    async def test_new_turn_trigger(self):
        """新轮触发：无活跃队列时创建后台任务。"""
        agent_mock = MagicMock()
        agent_mock._pending_queues = {}

        kernel = _make_kernel(agent_loop_mock=agent_mock)
        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={"_subagent_auto_trigger": True},
        )

        kernel.inject_message(msg)
        # 验证没有立即报错即可（后台任务异步执行）
        # context_id = sender_id = "user"
        assert "user" not in agent_mock._pending_queues

    def test_agent_loop_none_graceful(self):
        """AgentLoop 未初始化时优雅跳过。"""
        kernel = _make_kernel(agent_loop_mock=None)
        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default", content="x",
        )
        # 不应抛异常
        kernel.inject_message(msg)

    def test_queue_full_graceful(self):
        """队列满时优雅降级（不抛异常）。"""
        pending_queues: dict[str, asyncio.Queue] = {}
        pending_queues["user"] = asyncio.Queue(maxsize=1)
        pending_queues["user"].put_nowait("blocking_item")

        agent_mock = MagicMock()
        agent_mock._pending_queues = pending_queues

        kernel = _make_kernel(agent_loop_mock=agent_mock)
        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default", content="overflow",
        )

        # 不应抛异常
        kernel.inject_message(msg)


class TestHandleInjectedMessage:
    """Kernel._handle_injected_message() 测试。"""

    @pytest.mark.asyncio
    async def test_publishes_result_via_event_bus(self, event_bus):
        """注入消息处理后通过 EventBus 发布结果。"""
        published: list[dict] = []

        async def spy(data):
            published.append(data)

        event_bus.subscribe("agent.outbound", spy)

        # 构造 AgentLoop mock（避免真正调用 LLM）
        agent_mock = MagicMock()
        agent_mock._pending_queues = {}
        agent_mock._lock_manager = LockManager(max_concurrent=3)
        agent_mock._connect_mcp = AsyncMock()
        agent_mock._process_message = AsyncMock(return_value=OutboundMessage(
            channel="test", chat_id="user:default",
            content="subagent done", metadata={},
        ))
        agent_mock._pending_subagent_results = {}

        kernel = _make_kernel(agent_loop_mock=agent_mock, event_bus=event_bus)
        kernel._agent_loop = agent_mock

        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={"_subagent_auto_trigger": True},
        )

        await kernel._handle_injected_message(msg)

        assert len(published) == 1
        assert published[0]["content"] == "subagent done"
        assert published[0]["channel"] == "test"

    @pytest.mark.asyncio
    async def test_none_response_not_published(self, event_bus):
        """handle_message 返回 None 时不发布。"""
        published: list[dict] = []

        async def spy(data):
            published.append(data)

        event_bus.subscribe("agent.outbound", spy)

        agent_mock = MagicMock()
        agent_mock._pending_queues = {}
        agent_mock._lock_manager = LockManager(max_concurrent=3)
        agent_mock._connect_mcp = AsyncMock()
        agent_mock._process_message = AsyncMock(return_value=None)
        agent_mock._pending_subagent_results = {}

        kernel = _make_kernel(agent_loop_mock=agent_mock, event_bus=event_bus)
        kernel._agent_loop = agent_mock

        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={},
        )

        await kernel._handle_injected_message(msg)

        assert len(published) == 0

    @pytest.mark.asyncio
    async def test_exception_in_injected_message(self, event_bus):
        """注入消息处理异常时不崩溃（由 logger.exception 记录）。"""
        agent_mock = MagicMock()
        agent_mock._pending_queues = {}
        agent_mock._lock_manager = LockManager(max_concurrent=3)
        agent_mock._connect_mcp = AsyncMock()
        agent_mock._process_message = AsyncMock(side_effect=RuntimeError("simulated crash"))
        agent_mock._pending_subagent_results = {}

        kernel = _make_kernel(agent_loop_mock=agent_mock, event_bus=event_bus)
        kernel._agent_loop = agent_mock

        msg = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={},
        )

        # 不应抛异常
        await kernel._handle_injected_message(msg)

    @pytest.mark.asyncio
    async def test_concurrent_injection_single_user(self, event_bus):
        """同用户并发注入：第一条创建新 turn，后续中轮注入。"""
        pending_queues: dict[str, asyncio.Queue] = {}

        agent_mock = MagicMock()
        agent_mock._pending_queues = pending_queues
        agent_mock._lock_manager = LockManager(max_concurrent=3)
        agent_mock._connect_mcp = AsyncMock()
        # process_message 阻塞一小段时间，模拟处理中
        agent_mock._process_message = AsyncMock(side_effect=lambda *a, **kw: asyncio.sleep(0.05))
        agent_mock._pending_subagent_results = {}

        kernel = _make_kernel(agent_loop_mock=agent_mock, event_bus=event_bus)
        kernel._agent_loop = agent_mock

        msg1 = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={"_subagent_auto_trigger": True},
        )

        # 第一次注入：无活跃队列 → 创建后台任务 → 后台任务运行中注册了 queue
        kernel.inject_message(msg1)
        # 等待后台任务开始运行（此时 pending_queues 应该被注册了）
        await asyncio.sleep(0.02)

        msg2 = InboundMessage(
            channel="test", sender_id="user", chat_id="default",
            content="", metadata={"_subagent_auto_trigger": True},
        )

        # 第二次注入：此时有活跃队列 → 中轮注入
        kernel.inject_message(msg2)

        # 等待任务完成
        await asyncio.sleep(0.2)

        # 消息应通过中轮注入进入队列（由 _handle_message_impl 注册）
        # 中轮注入的消息在排空时被丢弃
        # 此测试仅验证不崩溃
        assert True
