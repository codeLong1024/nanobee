"""
Subagent 单元测试。

覆盖：SubagentManager 初始化、spawn、结果注入、EventBus 事件、
ContextVar 绑定、取消、运行计数、prompt 模板渲染。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.agent.subagent import SubagentManager, SubagentStatus
from nanobee.agent.tools.subagent import ListSubagentsTool, SpawnSubagentTool
from nanobee.kernel.context_sandbox_var import (
    RequestContext,
    bind_request_context,
    current_request_context,
    reset_request_context,
)
from nanobee.events.event_bus import EventBus
from nanobee.providers.base import LLMResponse


# =============================================================================
# Mock Provider
# =============================================================================


class _MockProvider:
    """模拟 LLM Provider，返回固定响应。"""

    def __init__(self) -> None:
        self.model = "test-model"

    def get_default_model(self) -> str:
        return self.model

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="mock result", finish_reason="stop")

    @property
    def generation(self) -> MagicMock:
        m = MagicMock()
        m.max_tokens = 4096
        return m


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def mock_provider() -> _MockProvider:
    return _MockProvider()


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def manager(
    mock_provider: _MockProvider,
    tmp_workspace: Path,
    event_bus: EventBus,
) -> SubagentManager:
    """创建 SubagentManager 实例。"""
    return SubagentManager(
        provider=mock_provider,
        workspace=tmp_workspace,
        model="test-model",
        event_bus=event_bus,
    )


@pytest.fixture
def collected_events(event_bus: EventBus) -> list[dict]:
    """收集所有 subagent.* 事件。"""
    events: list[dict] = []

    async def _handler(data: Any) -> None:
        events.append(data)

    event_bus.subscribe("subagent.spawned", _handler)
    event_bus.subscribe("subagent.ok", _handler)
    event_bus.subscribe("subagent.error", _handler)
    return events


# =============================================================================
# SubagentManager 基础功能
# =============================================================================


class TestSubagentManager:
    """SubagentManager 基础功能测试。"""

    def test_init(self, manager: SubagentManager) -> None:
        """创建时字段默认值正确。"""
        assert manager.model == "test-model"
        assert manager.max_iterations > 0
        assert manager.max_concurrent_subagents == 4
        assert manager.get_running_count() == 0

    def test_get_running_count_empty(self, manager: SubagentManager) -> None:
        """未 spawn 时运行计数为 0。"""
        assert manager.get_running_count() == 0
        assert manager.get_running_count_by_session("user-a") == 0

    @pytest.mark.asyncio
    async def test_spawn_returns_confirmation(
        self,
        manager: SubagentManager,
    ) -> None:
        """spawn 返回启动确认消息。"""
        result = await manager.spawn(
            task="do something",
            label="test-task",
            context_id="user-a",
        )
        assert "Subagent [test-task] started" in result
        assert "(id:" in result

    @pytest.mark.asyncio
    async def test_spawn_increases_running_count(
        self,
        manager: SubagentManager,
    ) -> None:
        """spawn 后运行计数增加。"""
        await manager.spawn(task="task-1", context_id="user-a")
        assert manager.get_running_count() == 1
        assert manager.get_running_count_by_session("user-a") == 1
        assert manager.get_running_count_by_session("user-b") == 0

    @pytest.mark.asyncio
    async def test_spawn_multiple_contexts(
        self,
        manager: SubagentManager,
    ) -> None:
        """不同用户的子代理独立计数。"""
        await manager.spawn(task="a-1", context_id="user-a")
        await manager.spawn(task="a-2", context_id="user-a")
        await manager.spawn(task="b-1", context_id="user-b")
        assert manager.get_running_count() == 3
        assert manager.get_running_count_by_session("user-a") == 2
        assert manager.get_running_count_by_session("user-b") == 1

    @pytest.mark.asyncio
    async def test_cancel_by_session(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
    ) -> None:
        """取消指定上下文的所有子代理。"""
        # run 完成后等待，让子代理在取消前保持 running
        async def _slow_run(spec: Any) -> MagicMock:
            await asyncio.sleep(10)  # 长时间不返回

        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
        )
        mgr.runner.run = _slow_run

        await mgr.spawn(task="a-1", context_id="user-a")
        await mgr.spawn(task="a-2", context_id="user-a")
        await mgr.spawn(task="b-1", context_id="user-b")
        assert mgr.get_running_count() == 3

        cancelled = await mgr.cancel_by_session("user-a")
        assert cancelled == 2
        await asyncio.sleep(0.05)

        # user-b 的子代理仍在运行（未被取消）
        assert mgr.get_running_count_by_session("user-b") == 1


# =============================================================================
# Subagent 结果注入
# =============================================================================


class TestSubagentResultInjection:
    """子代理结果注入回调测试。"""

    @pytest.mark.asyncio
    async def test_result_injector_called(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
    ) -> None:
        """子代理完成时调用 result_injector。"""
        injector = AsyncMock()
        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
            result_injector=injector,
        )
        # 确保 runner.run 快速返回
        mgr.runner.run = AsyncMock(return_value=MagicMock(
            final_content="done",
            stop_reason="completed",
            tool_events=[],
            messages=[],
            tools_used=[],
            usage={},
            error=None,
            had_injections=False,
        ))

        await mgr.spawn(task="test", context_id="user-a")
        # 等待子代理执行完成
        await asyncio.sleep(0.1)

        # injector 至少被调用一次
        assert injector.called, "result_injector should have been called"

    @pytest.mark.asyncio
    async def test_injector_content_format(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
    ) -> None:
        """注入内容包含子代理结果。"""
        captured: list[str] = []

        async def _injector(content: str, ctx_id: str, metadata: dict) -> None:
            captured.append(content)

        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
            result_injector=_injector,
        )
        mgr.runner.run = AsyncMock(return_value=MagicMock(
            final_content="completed task",
            stop_reason="completed",
            tool_events=[],
            messages=[],
            tools_used=[],
            usage={},
            error=None,
            had_injections=False,
        ))

        await mgr.spawn(task="do research", context_id="user-a")
        await asyncio.sleep(0.1)

        assert len(captured) >= 1
        assert "completed task" in captured[0]

    @pytest.mark.asyncio
    async def test_error_result(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
    ) -> None:
        """子代理出错时注入错误信息。"""
        captured: list[str] = []

        async def _injector(content: str, ctx_id: str, metadata: dict) -> None:
            captured.append(content)

        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
            result_injector=_injector,
        )
        mgr.runner.run = AsyncMock(return_value=MagicMock(
            final_content="error occurred",
            stop_reason="error",
            tool_events=[],
            messages=[],
            tools_used=[],
            usage={},
            error="Error: something went wrong",
            had_injections=False,
        ))

        await mgr.spawn(task="risky op", context_id="user-a")
        await asyncio.sleep(0.1)

        assert len(captured) >= 1
        assert "Error" in captured[0] or "failed" in captured[0]


# =============================================================================
# EventBus 事件
# =============================================================================


class TestSubagentEvents:
    """Subagent EventBus 事件测试。"""

    @pytest.mark.asyncio
    async def test_spawn_event(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
        event_bus: EventBus,
        collected_events: list[dict],
    ) -> None:
        """spawn 时发布 subagent.spawned 事件。"""
        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
            event_bus=event_bus,
        )

        await mgr.spawn(task="test", label="my-task", context_id="user-a")

        # 检查 spawned 事件
        spawned = [e for e in collected_events if e.get("label") == "my-task"]
        assert len(spawned) >= 1
        assert spawned[0]["context_id"] == "user-a"

    @pytest.mark.asyncio
    async def test_completion_event(
        self,
        mock_provider: _MockProvider,
        tmp_workspace: Path,
        event_bus: EventBus,
    ) -> None:
        """子代理完成时发布 subagent.ok / subagent.error 事件。"""
        events: list[dict] = []

        async def _handler(data: Any) -> None:
            events.append(data)

        event_bus.subscribe("subagent.ok", _handler)
        event_bus.subscribe("subagent.error", _handler)

        mgr = SubagentManager(
            provider=mock_provider,
            workspace=tmp_workspace,
            model="test-model",
            event_bus=event_bus,
        )
        mgr.runner.run = AsyncMock(return_value=MagicMock(
            final_content="done",
            stop_reason="completed",
            tool_events=[],
            messages=[],
            tools_used=[],
            usage={},
            error=None,
            had_injections=False,
        ))

        await mgr.spawn(task="success", context_id="user-a")
        await asyncio.sleep(0.15)

        # 应该有 subagent.ok 事件
        ok_events = [e for e in events if "label" in e]
        assert len(ok_events) >= 1


# =============================================================================
# Subagent 工具
# =============================================================================


class TestSubagentTools:
    """SpawnSubagentTool / ListSubagentsTool 测试。"""

    def test_spawn_tool_schema(self, manager: SubagentManager) -> None:
        """spawn_subagent 工具 schema 格式正确。"""
        tool = SpawnSubagentTool(manager)
        schema = tool.to_schema()
        func = schema["function"]
        assert func["name"] == "spawn_subagent"
        assert "task" in func["parameters"]["properties"]
        assert func["parameters"]["required"] == ["task"]

    def test_list_tool_schema(self, manager: SubagentManager) -> None:
        """list_subagents 工具 schema 格式正确。"""
        tool = ListSubagentsTool(manager)
        schema = tool.to_schema()
        assert schema["function"]["name"] == "list_subagents"

    @pytest.mark.asyncio
    async def test_spawn_execute(self, manager: SubagentManager) -> None:
        """执行 spawn_subagent 返回启动确认。"""
        tool = SpawnSubagentTool(manager)
        result = await tool.execute(task="test task")
        assert "started" in result.lower()

    @pytest.mark.asyncio
    async def test_list_execute_empty(self, manager: SubagentManager) -> None:
        """执行 list_subagents 返回空状态。"""
        tool = ListSubagentsTool(manager)
        result = await tool.execute()
        assert "0" in result


# =============================================================================
# ContextVar 绑定
# =============================================================================


class TestSubagentContextVar:
    """RequestContext ContextVar 测试（统一路由上下文）。"""

    def test_default_none(self) -> None:
        """未绑定时返回 None。"""
        assert current_request_context() is None

    def test_bind_and_reset(self) -> None:
        """bind/reset 生命周期正确，所有字段可读取。"""
        rctx = RequestContext(
            channel="channel_dingtalk",
            chat_id="shenqla",
            context_id="shenqla",
            session_id="dingtalk:shenqla",
        )
        token = bind_request_context(rctx)
        retrieved = current_request_context()
        assert retrieved is not None
        assert retrieved.channel == "channel_dingtalk"
        assert retrieved.chat_id == "shenqla"
        assert retrieved.context_id == "shenqla"
        assert retrieved.session_id == "dingtalk:shenqla"
        reset_request_context(token)
        assert current_request_context() is None

    def test_nested_bind(self) -> None:
        """嵌套绑定正确恢复，包括不同 session_id。"""
        rctx_a = RequestContext("ch-a", "chat-a", "ctx-a", "session-a")
        rctx_b = RequestContext("ch-b", "chat-b", "ctx-b", "session-b")
        token1 = bind_request_context(rctx_a)
        token2 = bind_request_context(rctx_b)
        assert current_request_context().session_id == "session-b"
        reset_request_context(token2)
        assert current_request_context().session_id == "session-a"
        reset_request_context(token1)
        assert current_request_context() is None


# =============================================================================
# SubagentStatus
# =============================================================================


class TestSubagentStatus:
    """SubagentStatus 数据类测试。"""

    def test_default_fields(self) -> None:
        """默认字段值正确。"""
        status = SubagentStatus(
            task_id="abc123",
            label="test",
            task_description="test task",
            started_at=100.0,
        )
        assert status.task_id == "abc123"
        assert status.label == "test"
        assert status.phase == "initializing"
        assert status.iteration == 0
        assert status.tool_events == []
        assert status.usage == {}
        assert status.stop_reason is None
        assert status.error is None

    def test_phase_transition(self) -> None:
        """phase 字段可更新。"""
        status = SubagentStatus(
            task_id="t1", label="l1",
            task_description="d1", started_at=0,
        )
        status.phase = "done"
        assert status.phase == "done"

    def test_error_set(self) -> None:
        """error 字段可设置。"""
        status = SubagentStatus(
            task_id="t1", label="l1",
            task_description="d1", started_at=0,
        )
        status.error = "something broke"
        assert status.error == "something broke"


# =============================================================================
# 模板渲染
# =============================================================================


class TestSubagentTemplates:
    """subagent prompt 模板渲染测试。"""

    def test_system_template_renders(self) -> None:
        """subagent_system.md 模板可渲染。"""
        from nanobee.utils.prompt_templates import render_template
        result = render_template(
            "agent/subagent_system.md",
            time_ctx="2026-06-23 10:35",
            workspace="/tmp/ws",
            skills_summary="- test-skill: a test skill",
        )
        assert "Subagent" in result
        assert "/tmp/ws" in result
        assert "test-skill" in result

    def test_announce_template_renders(self) -> None:
        """subagent_announce.md 模板可渲染。"""
        from nanobee.utils.prompt_templates import render_template
        result = render_template(
            "agent/subagent_announce.md",
            label="research",
            status_text="completed successfully",
            task="do research",
            result="found the answer",
        )
        assert "research" in result
        assert "completed successfully" in result
        assert "found the answer" in result
