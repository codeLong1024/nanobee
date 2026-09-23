"""``supports_push`` 能力声明与 cron 如实报告投递失败。

背景：HTTP 通道是 pull 模型（``send()`` 为空实现），事件型出站（cron 结果 /
kernel 注入 / 子代理通知）无法送达，但此前 cron 只判"事件是否发布成功"，
于是静默丢弃却报投递成功。本次新增声明式能力，让发布侧如实报告。

语义边界（爆炸半径控制）：
- 只有"已知且明确声明 ``supports_push=False``"才判失败；
- 目标通道未知/未加载、无 plugin_manager、无有效投递目标 → 保持现状（视为成功）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.builtin.tool_cron.plugin import ToolCronPlugin
from nanobee.builtin.tool_cron.types import (
    CronJob,
    CronJobError,
    CronPayload,
    CronSchedule,
)
from nanobee.channel.base import ChannelPlugin
from nanobee.plugins.base import PluginMetadata


def _make_job() -> CronJob:
    return CronJob(
        id="job_push",
        name="weekly-report",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            message="执行任务",
            channel="channel_http",
            to="user_a",
            user_id="user_a",
        ),
    )


def _make_plugin(
    tmp_path: Path,
    *,
    target_plugin: object | None,
    manager_present: bool = True,
    handle_message_return: object = None,
) -> ToolCronPlugin:
    """构造 cron 插件；``target_plugin`` 为 ``None`` 时模拟"通道查不到"。"""
    plugin = ToolCronPlugin(PluginMetadata(name="tool_cron", plugin_type="tool"))

    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.agent_loop = MagicMock()
    kernel.event_bus = AsyncMock()
    kernel.agent_loop.event_bus = AsyncMock()
    kernel.handle_message = AsyncMock(return_value=handle_message_return)

    if manager_present:
        manager = MagicMock()
        manager.get = MagicMock(return_value=target_plugin)
        kernel.plugin_manager = manager
    else:
        kernel.plugin_manager = None

    plugin.initialize(kernel)
    plugin._default_timezone = "UTC"
    return plugin


def _response(content: str = "周报已生成") -> SimpleNamespace:
    return SimpleNamespace(content=content, media=[], metadata={})


# ============================================================
# 能力声明
# ============================================================


class TestCapabilityDeclaration:
    def test_base_default_is_pushable(self) -> None:
        assert ChannelPlugin.supports_push is True

    def test_http_channel_declares_not_pushable(self) -> None:
        from nanobee.builtin.channel_http.plugin import HTTPChannelPlugin

        assert HTTPChannelPlugin.supports_push is False

    def test_push_capable_channels_keep_default(self) -> None:
        """CLI / 钉钉均为 push 模型，能力声明保持默认 True（行为零变化）。"""
        from nanobee.builtin.channel_cli.plugin import ChannelCLIPlugin
        from nanobee.builtin.channel_dingtalk.channel import DingTalkChannelPlugin

        assert ChannelCLIPlugin.supports_push is True
        assert DingTalkChannelPlugin.supports_push is True


# ============================================================
# _deliver：只有明确声明不可推送才判失败
# ============================================================


class TestDeliverCapabilityCheck:
    @pytest.mark.asyncio
    async def test_non_pushable_target_reports_failure(self, tmp_path: Path) -> None:
        target = SimpleNamespace(supports_push=False)
        plugin = _make_plugin(tmp_path, target_plugin=target)

        delivered = await plugin._deliver(_make_job(), "内容")

        assert delivered is False
        plugin.kernel.agent_loop.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pushable_target_publishes_as_before(self, tmp_path: Path) -> None:
        target = SimpleNamespace(supports_push=True)
        plugin = _make_plugin(tmp_path, target_plugin=target)

        delivered = await plugin._deliver(_make_job(), "内容")

        assert delivered is True
        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_channel_keeps_current_behaviour(self, tmp_path: Path) -> None:
        """通道未加载（查不到实例）→ 按现状视为可投递。"""
        plugin = _make_plugin(tmp_path, target_plugin=None)

        delivered = await plugin._deliver(_make_job(), "内容")

        assert delivered is True
        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plugin_without_declaration_is_pushable(self, tmp_path: Path) -> None:
        """第三方通道未声明该属性 → 按默认 True 处理（不破兼容）。"""
        plugin = _make_plugin(tmp_path, target_plugin=object())

        delivered = await plugin._deliver(_make_job(), "内容")

        assert delivered is True
        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_plugin_manager_keeps_current_behaviour(self, tmp_path: Path) -> None:
        plugin = _make_plugin(tmp_path, target_plugin=None, manager_present=False)

        delivered = await plugin._deliver(_make_job(), "内容")

        assert delivered is True
        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()


# ============================================================
# 端到端：任务状态语义
# ============================================================


class TestJobExecutionSemantics:
    @pytest.mark.asyncio
    async def test_non_pushable_target_raises_delivery_failure(self, tmp_path: Path) -> None:
        """执行成功但目标通道不能推送 → 抛 CronJobError（任务状态如实变红）。"""
        target = SimpleNamespace(supports_push=False)
        plugin = _make_plugin(
            tmp_path, target_plugin=target, handle_message_return=_response(),
        )

        with pytest.raises(CronJobError, match="结果投递失败"):
            await plugin._on_job_execute(_make_job())

    @pytest.mark.asyncio
    async def test_pushable_target_success_path_unchanged(self, tmp_path: Path) -> None:
        """可推送通道：成功路径逐字不变（返回正文、正常发布）。"""
        target = SimpleNamespace(supports_push=True)
        plugin = _make_plugin(
            tmp_path, target_plugin=target, handle_message_return=_response(),
        )

        result = await plugin._on_job_execute(_make_job())

        assert result == "周报已生成"
        plugin.kernel.agent_loop.event_bus.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_target_is_still_skipped(self, tmp_path: Path) -> None:
        """无有效投递目标 → 跳过视为成功（本次不改该现状）。"""
        target = SimpleNamespace(supports_push=False)
        plugin = _make_plugin(
            tmp_path, target_plugin=target, handle_message_return=_response(),
        )
        job = _make_job()
        job.payload.channel = ""

        result = await plugin._on_job_execute(job)

        assert result == "周报已生成"
        plugin.kernel.agent_loop.event_bus.publish.assert_not_awaited()
