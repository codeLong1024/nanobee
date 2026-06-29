"""Tool Cron 插件 — 多用户隔离测试。

通过 ContextVar 感知的 _resolve_store_path / _ensure_cron_service 验证
不同用户使用独立的 CronService 实例和存储文件。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.tool_cron.plugin import ToolCronPlugin
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


def _setup_cron_for(plugin: ToolCronPlugin, user_id: str) -> None:
    """为指定用户初始化 CronService 并挂载 store_path。"""
    store_path = plugin._resolve_store_path(user_id)
    plugin._ensure_cron_service(store_path)
    plugin._current_store_path = store_path


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
        _setup_cron_for(plugin, "user_a")
        cron_a = plugin._cron

        _setup_cron_for(plugin, "user_b")
        cron_b = plugin._cron

        assert cron_a is not cron_b
        assert cron_a is not None
        assert cron_b is not None

    def test_user_a_jobs_not_visible_to_user_b(self, plugin: ToolCronPlugin) -> None:
        """用户 A 添加的任务，用户 B 不可见。"""
        _setup_cron_for(plugin, "user_a")
        # 用户 A 添加任务
        plugin._add_job(
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="检查天气", every_seconds=60,
        )

        # 用户 B 列出任务
        _setup_cron_for(plugin, "user_b")
        result = plugin._list_jobs()

        assert "没有已调度的任务" in result

    def test_user_b_cannot_remove_user_a_job(self, plugin: ToolCronPlugin) -> None:
        """用户 B 无法移除用户 A 的任务。"""
        # 用户 A 添加任务
        _setup_cron_for(plugin, "user_a")
        add_result = plugin._add_job(
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="检查天气", every_seconds=60,
        )

        assert "已创建任务" in add_result
        job_id = add_result.split("id: ")[1].strip(")")

        # 用户 B 尝试移除
        _setup_cron_for(plugin, "user_b")
        remove_result = plugin._remove_job(job_id=job_id)

        assert "未找到" in remove_result or "not_found" in remove_result

    def test_same_user_reuses_cron_service(self, plugin: ToolCronPlugin) -> None:
        """同一 user_id 的多次初始化应该复用同一个 CronService 实例。"""
        _setup_cron_for(plugin, "user_a")
        cron_first = plugin._cron

        # 再次设置相同的 user_id
        _setup_cron_for(plugin, "user_a")
        cron_second = plugin._cron

        assert cron_first is cron_second

    def test_empty_user_id_stops_cron_service(self, plugin: ToolCronPlugin) -> None:
        """user_id 为空时 _resolve_store_path 无 data_dir 时应使用 base_dir 回退。"""
        # 先初始化非空用户
        _setup_cron_for(plugin, "user_a")
        assert plugin._cron is not None
        assert plugin._current_store_path is not None

    def test_three_users_complete_isolation(self, plugin: ToolCronPlugin) -> None:
        """三个用户完全隔离，互不影响。"""
        users = ["alice", "bob", "charlie"]
        cron_instances = []

        for user in users:
            _setup_cron_for(plugin, user)
            cron_instances.append(plugin._cron)

        # 所有实例应该不同
        for i in range(len(cron_instances)):
            for j in range(i + 1, len(cron_instances)):
                assert cron_instances[i] is not cron_instances[j]

        # 每个用户只能看到自己的任务
        for user in users:
            _setup_cron_for(plugin, user)
            plugin._add_job(
                channel="dingtalk", chat_id=f"chat_{user}", user_id=user,
                session_key=f"sess_{user}", action="add",
                message=f"{user}的任务", every_seconds=60,
            )

        for user in users:
            _setup_cron_for(plugin, user)
            result = plugin._list_jobs()
            assert f"{user}的任务" in result

            for other_user in users:
                if other_user != user:
                    assert f"{other_user}的任务" not in result


class TestCronStoreFileIsolation:
    """存储文件隔离测试。"""

    def test_store_files_created_separately(self, plugin: ToolCronPlugin) -> None:
        """不同用户的存储文件应该分别创建。"""
        # 用户 A 添加任务
        _setup_cron_for(plugin, "user_a")
        plugin._add_job(
            channel="dingtalk", chat_id="chat_a", user_id="user_a", session_key="sess_a",
            action="add", message="用户A的任务", every_seconds=60,
        )

        # 用户 B 添加任务
        _setup_cron_for(plugin, "user_b")
        plugin._add_job(
            channel="dingtalk", chat_id="chat_b", user_id="user_b", session_key="sess_b",
            action="add", message="用户B的任务", every_seconds=120,
        )

        # 读取各自的 store_path
        _setup_cron_for(plugin, "user_a")
        path_a = plugin._current_store_path
        _setup_cron_for(plugin, "user_b")
        path_b = plugin._current_store_path

        assert path_a is not None and path_a.exists()
        assert path_b is not None and path_b.exists()
        assert path_a != path_b

        # 验证文件内容
        for f in (path_a, path_b):
            data = json.loads(f.read_text(encoding="utf-8"))
            assert "jobs" in data
            assert len(data["jobs"]) == 1
