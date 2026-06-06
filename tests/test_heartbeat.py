"""HeartbeatService 单元测试

覆盖核心功能:
- 文件读取
- 决策逻辑 (_decide)
- 结果过滤 (_is_deliverable)
- 手动触发 (trigger_now)
- 生命周期 (start/stop)
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.heartbeat.service import HeartbeatService

import logging
logging.basicConfig(level=logging.INFO)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """临时工作目录"""
    return tmp_path


@pytest.fixture
def workflow_file(workspace: Path) -> Path:
    """创建 WORKFLOW.md 文件"""
    workflow = workspace / "WORKFLOW.md"
    workflow.write_text("待处理任务:\n- 检查系统状态\n- 汇报结果", encoding="utf-8")
    return workflow


@pytest.fixture
def mock_provider() -> MagicMock:
    """模拟 LLM Provider"""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()

    # 默认返回 skip 决策
    mock_response = MagicMock()
    mock_response.should_execute_tools = False
    mock_response.has_tool_calls = False
    provider.chat_with_retry.return_value = mock_response

    return provider


@pytest.fixture
def heartbeat_service(
    workspace: Path,
    mock_provider: MagicMock,
) -> MagicMock:
    """创建 HeartbeatService 实例"""
    from nanobee.heartbeat.service import HeartbeatService

    service = HeartbeatService(
        workspace=workspace,
        provider=mock_provider,
        model="test-model",
        interval_s=60,
        enabled=True,
    )

    # 注入 mock LLM provider
    service._llm_provider = mock_provider

    return service


class TestHeartbeatService:
    """HeartbeatService 测试类"""

    def test_is_deliverable_valid(self):
        """测试有效结果可投递"""
        valid_response = "系统状态正常,所有服务运行良好"
        assert HeartbeatService._is_deliverable(valid_response) is True

    def test_is_deliverable_fallback(self):
        """测试 Runner 最终化回退被过滤"""
        fallback_response = "couldn't produce a final answer, retrying..."
        assert HeartbeatService._is_deliverable(fallback_response) is False

    def test_is_deliverable_leaked_reasoning(self):
        """测试泄露内部推理被过滤"""
        leaked_response = "根据 workflow.md 中的决策逻辑..."
        assert HeartbeatService._is_deliverable(leaked_response) is False

    def test_is_deliverable_meta_commentary(self):
        """测试元评论被过滤"""
        meta_response = "I am supposed to follow strict heartbeat interpretation"
        assert HeartbeatService._is_deliverable(meta_response) is False

    def test_read_workflow_file_exists(self, heartbeat_service, workflow_file):
        """测试读取存在的 WORKFLOW.md"""
        content = heartbeat_service._read_workflow_file()
        assert content is not None
        assert "检查系统状态" in content

    def test_read_workflow_file_missing(self, workspace, mock_provider):
        """测试读取不存在的 WORKFLOW.md"""
        from nanobee.heartbeat.service import HeartbeatService

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
        )
        content = service._read_workflow_file()
        assert content is None

    @pytest.mark.asyncio
    async def test_decide_skip(self, heartbeat_service, workflow_file, mock_provider):
        """测试决策:跳过 (无任务)"""
        # 模拟 LLM 返回 skip
        mock_response = MagicMock()
        mock_response.should_execute_tools = False
        mock_response.has_tool_calls = False
        mock_provider.chat_with_retry.return_value = mock_response

        action, tasks = await heartbeat_service._decide("无任务")
        assert action == "skip"
        assert tasks == ""

    @pytest.mark.asyncio
    async def test_decide_run(self, heartbeat_service, workflow_file, mock_provider):
        """测试决策:执行 (有任务)"""
        # 模拟 LLM 返回 run
        mock_response = MagicMock()
        mock_response.should_execute_tools = True
        mock_response.has_tool_calls = True
        mock_response.tool_calls = [MagicMock()]
        mock_response.tool_calls[0].arguments = {
            "action": "run",
            "tasks": "检查系统状态并汇报",
        }
        mock_provider.chat_with_retry.return_value = mock_response

        action, tasks = await heartbeat_service._decide(workflow_file.read_text())
        assert action == "run"
        assert "检查系统状态" in tasks

    @pytest.mark.asyncio
    async def test_trigger_now_no_workflow(self, workspace, mock_provider):
        """测试手动触发:无 WORKFLOW.md"""
        from nanobee.heartbeat.service import HeartbeatService

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
        )
        result = await service.trigger_now()
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_now_skip(self, workspace, workflow_file, mock_provider):
        """测试手动触发:决策跳过"""
        from nanobee.heartbeat.service import HeartbeatService

        mock_response = MagicMock()
        mock_response.should_execute_tools = False
        mock_response.has_tool_calls = False
        mock_provider.chat_with_retry.return_value = mock_response

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
        )
        service._llm_provider = mock_provider

        result = await service.trigger_now()
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_now_run(self, workspace, workflow_file, mock_provider):
        """测试手动触发:决策执行"""
        from nanobee.heartbeat.service import HeartbeatService

        mock_response = MagicMock()
        mock_response.should_execute_tools = True
        mock_response.has_tool_calls = True
        mock_response.tool_calls = [MagicMock()]
        mock_response.tool_calls[0].arguments = {
            "action": "run",
            "tasks": "执行任务",
        }
        mock_provider.chat_with_retry.return_value = mock_response

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
            on_execute=AsyncMock(return_value="任务完成"),
        )
        service._llm_provider = mock_provider

        result = await service.trigger_now()
        assert result == "任务完成"

    @pytest.mark.asyncio
    async def test_start_stop(self, workspace, mock_provider):
        """测试启动/停止生命周期"""
        from nanobee.heartbeat.service import HeartbeatService

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
            interval_s=60,
            enabled=True,
        )
        service._llm_provider = mock_provider

        await service.start()
        assert service._running is True

        service.stop()
        assert service._running is False

    @pytest.mark.asyncio
    async def test_start_disabled(self, workspace, mock_provider):
        """测试禁用状态下启动"""
        from nanobee.heartbeat.service import HeartbeatService

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
            enabled=False,
        )

        await service.start()
        assert service._running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self, workspace, mock_provider):
        """测试重复启动"""
        from nanobee.heartbeat.service import HeartbeatService

        service = HeartbeatService(
            workspace=workspace,
            provider=mock_provider,
            model="test-model",
            enabled=True,
        )
        service._llm_provider = mock_provider

        await service.start()
        assert service._running is True

        # 重复启动应记录警告但不改变状态
        await service.start()
        assert service._running is True


class TestHeartbeatServiceDeliverablePatterns:
    """测试 _is_deliverable 的各种泄露模式"""

    def test_leaked_awareness_md(self):
        """测试 awareness.md 泄露"""
        response = "根据 awareness.md 中的信息..."
        assert HeartbeatService._is_deliverable(response) is False

    def test_leaked_judgment_call(self):
        """测试 judgment call 泄露"""
        response = "Judgment call: the task is important"
        assert HeartbeatService._is_deliverable(response) is False

    def test_leaked_valid_options(self):
        """测试 valid options 泄露"""
        response = "Valid options are: skip, run, pause"
        assert HeartbeatService._is_deliverable(response) is False

    def test_leaked_my_instructions(self):
        """测试 my instructions 泄露"""
        response = "My instructions say I should..."
        assert HeartbeatService._is_deliverable(response) is False

    def test_clean_response_with_task_name(self):
        """测试包含任务名的干净响应"""
        response = "已完成文件系统的检查,生成报告"
        assert HeartbeatService._is_deliverable(response) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
