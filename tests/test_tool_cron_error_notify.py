"""Tool Cron 插件 — 任务执行错误通知测试。

验证 _on_job_execute 的错误识别与透传机制：
- agent 内部错误（turn_internal_error 系统通知）→ 补任务标识投递 + raise CronJobError
- handle_message 抛异常 → 投递错误通知（suppress 保护）+ 原样上抛
- 投递失败不遮蔽原始异常
- 正常路径不受影响（返回 content，metadata 无错误标记）
- 空结果（None / 空 content）保持静默
- service._execute_job 捕获 CronJobError 后记录 last_status="error"
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.builtin.tool_cron.plugin import ToolCronPlugin
from nanobee.builtin.tool_cron.service import CronService
from nanobee.builtin.tool_cron.types import CronJob, CronJobError, CronPayload, CronSchedule
from nanobee.plugins.base import PluginMetadata


def _make_job() -> CronJob:
    """构造带有效投递目标的测试任务。"""
    return CronJob(
        id="job_err",
        name="weather-monitor",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            message="执行任务",
            channel="dingtalk",
            to="user_a",
            user_id="user_a",
        ),
    )


def _make_plugin(
    tmp_path: Path,
    handle_message_return: object = None,
    handle_message_side_effect: Exception | None = None,
) -> ToolCronPlugin:
    """构造带可用 kernel/agent_loop/event_bus 的插件实例。"""
    plugin = ToolCronPlugin(PluginMetadata(name="tool_cron", plugin_type="tool"))

    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.agent_loop = MagicMock()
    kernel.event_bus = AsyncMock()
    kernel.agent_loop.event_bus = AsyncMock()
    if handle_message_side_effect is not None:
        kernel.handle_message = AsyncMock(side_effect=handle_message_side_effect)
    else:
        kernel.handle_message = AsyncMock(return_value=handle_message_return)

    plugin.initialize(kernel)
    plugin._default_timezone = "UTC"
    return plugin


def _make_error_response() -> MagicMock:
    """模拟 handle_message 返回的 turn_internal_error 系统通知。

    对应 loop.py _state_respond 的错误产物：
    metadata 携带 notification_type=system + severity=error + error_detail。
    """
    resp = MagicMock()
    resp.content = "抱歉，处理消息时发生内部错误，请稍后重试或联系管理员。"
    resp.metadata = {
        "notification_type": "system",
        "notification_kind": "turn_internal_error",
        "severity": "error",
        "error_detail": "Error: RuntimeError: LLM 调用失败",
    }
    return resp


class TestAgentErrorNotification:
    """agent 层内部错误（turn_internal_error）识别与投递测试。"""

    @pytest.mark.asyncio
    async def test_delivers_notice_with_job_identity(self, tmp_path: Path) -> None:
        """错误通知投递内容必须含任务名 + 任务 ID + 错误详情。"""
        plugin = _make_plugin(tmp_path, handle_message_return=_make_error_response())
        job = _make_job()

        with pytest.raises(CronJobError, match="LLM 调用失败"):
            await plugin._on_job_execute(job)

        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()
        event, data = plugin.kernel.agent_loop.event_bus.publish.await_args.args
        assert event == "agent.outbound"
        assert "weather-monitor" in data["content"]
        assert "job_err" in data["content"]
        assert "LLM 调用失败" in data["content"]

    @pytest.mark.asyncio
    async def test_notice_metadata_carries_system_error_marker(self, tmp_path: Path) -> None:
        """错误通知 metadata 必须携带系统通知标记，供通道差异化渲染。"""
        plugin = _make_plugin(tmp_path, handle_message_return=_make_error_response())
        job = _make_job()

        with pytest.raises(CronJobError):
            await plugin._on_job_execute(job)

        data = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert data["metadata"]["notification_type"] == "system"
        assert data["metadata"]["notification_kind"] == "cron_job_error"
        assert data["metadata"]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_raises_cron_job_error_for_service_state(self, tmp_path: Path) -> None:
        """错误场景抛出 CronJobError，让 service 层记录失败状态。"""
        plugin = _make_plugin(tmp_path, handle_message_return=_make_error_response())
        job = _make_job()

        with pytest.raises(CronJobError):
            await plugin._on_job_execute(job)


class TestExceptionNotification:
    """handle_message 调用异常场景测试。"""

    @pytest.mark.asyncio
    async def test_exception_delivers_notice_and_reraises(self, tmp_path: Path) -> None:
        """调用异常：仍投递带任务标识的错误通知，并原样上抛。"""
        plugin = _make_plugin(tmp_path, handle_message_side_effect=RuntimeError("boom"))
        job = _make_job()

        with pytest.raises(RuntimeError, match="boom"):
            await plugin._on_job_execute(job)

        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()
        data = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert "weather-monitor" in data["content"]
        assert "job_err" in data["content"]
        assert "RuntimeError" in data["content"]
        assert data["metadata"]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_deliver_failure_does_not_mask_original_exception(self, tmp_path: Path) -> None:
        """错误通知投递失败时，原始异常不被遮蔽（suppress 保护）。"""
        plugin = _make_plugin(tmp_path, handle_message_side_effect=RuntimeError("boom"))
        plugin.kernel.agent_loop.event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish fail")
        )
        job = _make_job()

        with pytest.raises(RuntimeError, match="boom"):
            await plugin._on_job_execute(job)

    @pytest.mark.asyncio
    async def test_agent_error_raise_survives_deliver_failure(self, tmp_path: Path) -> None:
        """agent 错误路径：投递失败不得跳过 CronJobError（service 记录失败状态的唯一依据）。"""
        plugin = _make_plugin(tmp_path, handle_message_return=_make_error_response())
        plugin.kernel.agent_loop.event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish fail")
        )
        job = _make_job()

        with pytest.raises(CronJobError, match="LLM 调用失败"):
            await plugin._on_job_execute(job)


class TestSuccessPathUnchanged:
    """成功路径回归测试。"""

    @pytest.mark.asyncio
    async def test_success_returns_content_without_error_marker(self, tmp_path: Path) -> None:
        """成功回复：返回 content，metadata 不携带系统错误标记。"""
        resp = MagicMock()
        resp.content = "任务完成"
        resp.metadata = {"foo": "bar"}
        plugin = _make_plugin(tmp_path, handle_message_return=resp)
        job = _make_job()

        result = await plugin._on_job_execute(job)

        assert result == "任务完成"
        data = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert "notification_type" not in data["metadata"]
        assert data["content"] == "任务完成"

    @pytest.mark.asyncio
    async def test_none_result_is_silent(self, tmp_path: Path) -> None:
        """handle_message 返回 None（空结果）：静默，不投递不报错。"""
        plugin = _make_plugin(tmp_path, handle_message_return=None)
        job = _make_job()

        result = await plugin._on_job_execute(job)

        assert result == ""
        plugin.kernel.agent_loop.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_content_is_silent(self, tmp_path: Path) -> None:
        """空 content 同样静默（正常 cron 任务可能无输出）。"""
        resp = MagicMock()
        resp.content = ""
        resp.metadata = {}
        plugin = _make_plugin(tmp_path, handle_message_return=resp)
        job = _make_job()

        result = await plugin._on_job_execute(job)

        assert result == ""
        plugin.kernel.agent_loop.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_delivery_failure_raises_distinct_error(self, tmp_path: Path) -> None:
        """真实 bug 回归：执行成功但投递失败，必须抛 CronJobError 且语义可区分。

        修复前：publish 异常直接冒泡到 service，任务被标 error 但 last_error 是
        英文异常串、全程无堆栈，无法定位是执行失败还是投递失败。
        """
        resp = MagicMock()
        resp.content = "任务完成"
        resp.metadata = {}
        plugin = _make_plugin(tmp_path, handle_message_return=resp)
        plugin.kernel.agent_loop.event_bus.publish = AsyncMock(
            side_effect=RuntimeError("publish fail")
        )
        job = _make_job()

        with pytest.raises(CronJobError, match="结果投递失败"):
            await plugin._on_job_execute(job)


class TestServiceRecordsError:
    """service 层状态记录闭环测试。"""

    @pytest.mark.asyncio
    async def test_execute_job_records_error_state(self, tmp_path: Path) -> None:
        """CronJobError 被 _execute_job 捕获，last_status/last_error 如实记录。"""

        async def failing_on_job(job: CronJob) -> str | None:
            raise CronJobError("Error: RuntimeError: LLM 调用失败")

        cron = CronService(store_path=tmp_path / "jobs.json", on_job=failing_on_job)
        schedule = CronSchedule(kind="every", every_ms=60_000)
        job = cron.add_job("weather-monitor", schedule, "执行任务")

        await cron._execute_job(cron._store.jobs[0])

        assert job.state.last_status == "error"
        assert "LLM 调用失败" in (job.state.last_error or "")
        assert job.state.run_history[-1].status == "error"

    @pytest.mark.asyncio
    async def test_execute_job_records_ok_on_success(self, tmp_path: Path) -> None:
        """正常执行不受影响，last_status 记录 ok（回归保护）。"""

        async def ok_on_job(job: CronJob) -> str | None:
            return "done"

        cron = CronService(store_path=tmp_path / "jobs.json", on_job=ok_on_job)
        schedule = CronSchedule(kind="every", every_ms=60_000)
        job = cron.add_job("weather-monitor", schedule, "执行任务")

        await cron._execute_job(cron._store.jobs[0])

        assert job.state.last_status == "ok"
        assert job.state.last_error is None


class TestBuildErrorNotice:
    """错误通知文案构造测试。"""

    def test_notice_contains_job_identity(self) -> None:
        """文案必须包含任务名与任务 ID。"""
        job = _make_job()
        notice = ToolCronPlugin._build_error_notice(job, "something broke")

        assert "weather-monitor" in notice
        assert "job_err" in notice
        assert "something broke" in notice
