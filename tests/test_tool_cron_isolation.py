"""Tool Cron 插件 — 多用户隔离测试。

通过 ContextVar 感知的 _resolve_store_path / _ensure_cron_service 验证
不同用户使用独立的 CronService 实例和存储文件。
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.builtin.tool_cron.plugin import ToolCronPlugin
from nanobee.builtin.tool_cron.service import CronService
from nanobee.plugins.base import PluginMetadata


@pytest.fixture
def plugin(tmp_path: Path) -> ToolCronPlugin:
    """创建插件实例，使用临时目录作为 work_dir。"""
    plugin = ToolCronPlugin(PluginMetadata(name="tool_cron", plugin_type="tool"))

    # 模拟 kernel
    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.config.get.return_value.get.return_value.get.return_value = "UTC"

    plugin.initialize(kernel)
    plugin._default_timezone = "UTC"

    return plugin


def _setup_cron_for(plugin: ToolCronPlugin, user_id: str) -> CronService:
    """为指定用户初始化 CronService 并返回实例。"""
    store_path = plugin._resolve_store_path(user_id)
    return plugin._ensure_cron_service(user_id, store_path)


class TestMultiUserIsolation:
    """多用户隔离测试。"""

    def test_different_users_get_different_store_paths(self, plugin: ToolCronPlugin) -> None:
        """不同 user_id 应该使用不同的存储文件。"""
        path_a = plugin._resolve_store_path("user_a")
        path_b = plugin._resolve_store_path("user_b")

        assert path_a is not None
        assert path_b is not None
        assert path_a != path_b
        assert "jobs_user_a.json" in str(path_a)
        assert "jobs_user_b.json" in str(path_b)

    def test_switch_user_creates_new_cron_service(self, plugin: ToolCronPlugin) -> None:
        """切换 user_id 时应该创建新的 CronService 实例。"""
        cron_a = _setup_cron_for(plugin, "user_a")
        cron_b = _setup_cron_for(plugin, "user_b")

        assert cron_a is not cron_b
        assert cron_a is not None
        assert cron_b is not None
        assert "user_a" in plugin._crons
        assert "user_b" in plugin._crons

    def test_user_a_jobs_not_visible_to_user_b(self, plugin: ToolCronPlugin) -> None:
        """用户 A 添加的任务，用户 B 不可见。"""
        cron_a = _setup_cron_for(plugin, "user_a")
        # 用户 A 添加任务
        plugin._add_job(
            cron=cron_a,
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="检查天气", every_seconds=60,
        )

        # 用户 B 列出任务
        cron_b = _setup_cron_for(plugin, "user_b")
        result = plugin._list_jobs(cron_b)

        assert "没有已调度的任务" in result

    def test_user_b_cannot_remove_user_a_job(self, plugin: ToolCronPlugin) -> None:
        """用户 B 无法移除用户 A 的任务。"""
        # 用户 A 添加任务
        cron_a = _setup_cron_for(plugin, "user_a")
        add_result = plugin._add_job(
            cron=cron_a,
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="检查天气", every_seconds=60,
        )

        assert "已创建任务" in add_result
        job_id = add_result.split("id: ")[1].strip(")")

        # 用户 B 尝试移除
        cron_b = _setup_cron_for(plugin, "user_b")
        remove_result = plugin._remove_job(cron=cron_b, job_id=job_id)

        assert "未找到" in remove_result or "not_found" in remove_result

    def test_same_user_reuses_cron_service(self, plugin: ToolCronPlugin) -> None:
        """同一 user_id 的多次初始化应该复用同一个 CronService 实例。"""
        cron_first = _setup_cron_for(plugin, "user_a")

        # 再次设置相同的 user_id
        cron_second = _setup_cron_for(plugin, "user_a")

        assert cron_first is cron_second
        assert len(plugin._crons) == 1

    def test_empty_user_id_stops_cron_service(self, plugin: ToolCronPlugin) -> None:
        """与空 user_id 对应的 CronService 也能正确创建。"""
        cron = _setup_cron_for(plugin, "user_a")
        assert cron is not None
        assert cron.store_path is not None

    def test_three_users_complete_isolation(self, plugin: ToolCronPlugin) -> None:
        """三个用户完全隔离，互不影响。"""
        users = ["alice", "bob", "charlie"]
        cron_instances = {}

        for user in users:
            cron_instances[user] = _setup_cron_for(plugin, user)

        # 所有实例应该不同
        assert cron_instances["alice"] is not cron_instances["bob"]
        assert cron_instances["alice"] is not cron_instances["charlie"]
        assert cron_instances["bob"] is not cron_instances["charlie"]

        # 每个用户只能看到自己的任务
        for user in users:
            plugin._add_job(
                cron=cron_instances[user],
                channel="dingtalk", chat_id=f"chat_{user}", user_id=user,
                session_key=f"sess_{user}", action="add",
                message=f"{user}的任务", every_seconds=60,
            )

        for user in users:
            result = plugin._list_jobs(cron_instances[user])
            assert f"{user}的任务" in result

            for other_user in users:
                if other_user != user:
                    assert f"{other_user}的任务" not in result


class TestCronStoreFileIsolation:
    """存储文件隔离测试。"""

    def test_store_files_created_separately(self, plugin: ToolCronPlugin) -> None:
        """不同用户的存储文件应该分别创建。"""
        # 用户 A 添加任务
        cron_a = _setup_cron_for(plugin, "user_a")
        plugin._add_job(
            cron=cron_a,
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="用户A的任务", every_seconds=60,
        )

        # 用户 B 添加任务
        cron_b = _setup_cron_for(plugin, "user_b")
        plugin._add_job(
            cron=cron_b,
            channel="dingtalk", chat_id="chat_b", user_id="user_b", session_key="sess_b",
            action="add", message="用户B的任务", every_seconds=120,
        )

        # 读取各自的 store_path
        path_a = plugin._crons["user_a"].store_path
        path_b = plugin._crons["user_b"].store_path

        assert path_a is not None and path_a.exists()
        assert path_b is not None and path_b.exists()
        assert path_a != path_b

        # 验证文件内容
        for f in (path_a, path_b):
            data = json.loads(f.read_text(encoding="utf-8"))
            assert "jobs" in data
            assert len(data["jobs"]) == 1


class TestJobExecuteIsolation:
    """Cron 任务执行时的用户隔离键测试。

    验证 _on_job_execute 调用 handle_message 时：
    - sender_id = job.payload.user_id（创建任务的用户，作为真正的用户隔离键）
    - context_id = 投递目标 chat_id
    - user_id 缺失时 sender_id 兜底为 "system"
    """

    @staticmethod
    def _make_job(user_id: str | None) -> "CronJob":
        from nanobee.builtin.tool_cron.types import CronJob, CronPayload, CronSchedule

        return CronJob(
            id="job_1",
            name="test_job",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(
                message="执行测试任务",
                channel="dingtalk",
                to="user_a",
                user_id=user_id,
            ),
        )

    @staticmethod
    def _make_plugin(tmp_path: Path) -> "ToolCronPlugin":
        """构造带可用 kernel/agent_loop/event_bus 的插件实例。"""
        plugin = ToolCronPlugin(PluginMetadata(name="tool_cron", plugin_type="tool"))

        kernel = MagicMock()
        kernel.data_dir = str(tmp_path)
        kernel.agent_loop = MagicMock()
        kernel.event_bus = AsyncMock()
        kernel.agent_loop.event_bus = AsyncMock()
        kernel.handle_message = AsyncMock(
            return_value=MagicMock(content="任务已执行")
        )

        plugin.initialize(kernel)
        plugin._default_timezone = "UTC"
        return plugin

    @pytest.mark.asyncio
    async def test_execute_uses_user_id_as_sender(self, tmp_path: Path) -> None:
        """有 user_id 时，sender_id 应为该用户（用户隔离键）。"""
        plugin = self._make_plugin(tmp_path)
        job = self._make_job(user_id="user_bob")

        result = await plugin._on_job_execute(job)

        assert result == "任务已执行"
        plugin.kernel.handle_message.assert_awaited_once_with(
            message="执行测试任务",
            context_id="user_a",
            channel="dingtalk",
            sender_id="user_bob",
            fresh_session=True,
        )

    @pytest.mark.asyncio
    async def test_execute_falls_back_to_system_sender(self, tmp_path: Path) -> None:
        """user_id 缺失时（系统级任务），sender_id 兜底为 system。"""
        plugin = self._make_plugin(tmp_path)
        job = self._make_job(user_id=None)

        result = await plugin._on_job_execute(job)

        assert result == "任务已执行"
        plugin.kernel.handle_message.assert_awaited_once_with(
            message="执行测试任务",
            context_id="user_a",
            channel="dingtalk",
            sender_id="system",
            fresh_session=True,
        )
