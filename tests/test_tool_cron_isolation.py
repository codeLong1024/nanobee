"""Tool Cron 插件 — 多用户隔离测试。

验证不同 user_id 的 set_context 调用会切换到独立的 CronService 实例，
确保用户 A 无法操作用户 B 的定时任务。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.tool_cron.plugin import ToolCronPlugin


@pytest.fixture
def plugin(tmp_path: Path) -> ToolCronPlugin:
    """创建插件实例，使用临时目录作为 work_dir。"""
    plugin = ToolCronPlugin()

    # 模拟 kernel
    kernel = MagicMock()
    kernel.work_dir = str(tmp_path)
    # get_config 返回默认时区
    kernel.config.get.return_value.get.return_value.get.return_value = "UTC"

    plugin.initialize(kernel)

    # 确保 _default_timezone 是正确的值
    plugin._default_timezone = "UTC"

    return plugin


class TestMultiUserIsolation:
    """多用户隔离测试。"""

    def test_different_users_get_different_store_paths(self, plugin: ToolCronPlugin) -> None:
        """不同 user_id 应该使用不同的存储文件。"""
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        path_a = plugin._current_store_path

        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        path_b = plugin._current_store_path

        assert path_a is not None
        assert path_b is not None
        assert path_a != path_b
        assert "jobs_user_a.json" in str(path_a)
        assert "jobs_user_b.json" in str(path_b)

    def test_set_context_creates_new_cron_service(self, plugin: ToolCronPlugin) -> None:
        """切换 user_id 时应该创建新的 CronService 实例。"""
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        cron_a = plugin._cron

        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        cron_b = plugin._cron

        # 应该是不同的实例
        assert cron_a is not cron_b
        assert cron_a is not None
        assert cron_b is not None

    def test_user_a_jobs_not_visible_to_user_b(self, plugin: ToolCronPlugin) -> None:
        """用户 A 添加的任务，用户 B 不可见。"""
        # 用户 A 添加任务
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        plugin._add_job(
            action="add",
            message="检查天气",
            every_seconds=60,
        )

        # 用户 B 列出任务
        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        result = plugin._list_jobs()

        assert "没有已调度的任务" in result

    def test_user_b_cannot_remove_user_a_job(self, plugin: ToolCronPlugin) -> None:
        """用户 B 无法移除用户 A 的任务。"""
        # 用户 A 添加任务
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        add_result = plugin._add_job(
            action="add",
            message="检查天气",
            every_seconds=60,
        )

        # 提取 job_id
        assert "已创建任务" in add_result
        job_id = add_result.split("id: ")[1].strip(")")

        # 用户 B 尝试移除
        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        remove_result = plugin._remove_job(job_id=job_id)

        assert "未找到" in remove_result or "not_found" in remove_result

    def test_same_user_context_reuses_cron_service(self, plugin: ToolCronPlugin) -> None:
        """同一 user_id 的多次 set_context 应该复用同一个 CronService 实例。"""
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        cron_first = plugin._cron

        # 再次设置相同的 user_id
        plugin.set_context(channel="dingtalk", chat_id="chat_a_new", user_id="user_a")
        cron_second = plugin._cron

        # 应该复用同一个实例（store_path 相同）
        assert cron_first is cron_second

    def test_empty_user_id_stops_cron_service(self, plugin: ToolCronPlugin) -> None:
        """user_id 为空时应该停止并清空 CronService。"""
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        assert plugin._cron is not None

        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="")
        assert plugin._cron is None
        assert plugin._current_store_path is None

    def test_three_users_complete_isolation(self, plugin: ToolCronPlugin) -> None:
        """三个用户完全隔离，互不影响。"""
        users = ["alice", "bob", "charlie"]
        cron_instances = []

        for user in users:
            plugin.set_context(channel="dingtalk", chat_id=f"chat_{user}", user_id=user)
            cron_instances.append(plugin._cron)

        # 所有实例应该不同
        for i in range(len(cron_instances)):
            for j in range(i + 1, len(cron_instances)):
                assert cron_instances[i] is not cron_instances[j]

        # 每个用户只能看到自己的任务
        for idx, user in enumerate(users):
            plugin.set_context(channel="dingtalk", chat_id=f"chat_{user}", user_id=user)
            plugin._add_job(
                action="add",
                message=f"{user}的任务",
                every_seconds=60,
            )

        for idx, user in enumerate(users):
            plugin.set_context(channel="dingtalk", chat_id=f"chat_{user}", user_id=user)
            result = plugin._list_jobs()
            assert f"{user}的任务" in result

            # 验证看不到其他用户的任务
            for other_user in users:
                if other_user != user:
                    assert f"{other_user}的任务" not in result


class TestCronStoreFileIsolation:
    """存储文件隔离测试。"""

    def test_store_files_created_separately(self, plugin: ToolCronPlugin) -> None:
        """不同用户的存储文件应该分别创建。"""
        # 用户 A 添加任务（触发文件创建）
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        plugin._add_job(
            action="add",
            message="用户A的任务",
            every_seconds=60,
        )

        # 用户 B 添加任务（触发文件创建）
        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        plugin._add_job(
            action="add",
            message="用户B的任务",
            every_seconds=120,
        )

        # 验证两个文件都存在
        store_path_a = plugin._current_store_path
        store_path_b = plugin._current_store_path
        # 分别执行两次 set_context 拿到各自的 store_path
        plugin.set_context(channel="dingtalk", chat_id="chat_a", user_id="user_a")
        path_a = plugin._current_store_path
        plugin.set_context(channel="dingtalk", chat_id="chat_b", user_id="user_b")
        path_b = plugin._current_store_path

        assert path_a is not None and path_a.exists()
        assert path_b is not None and path_b.exists()
        assert path_a != path_b

        # 验证文件内容
        for f in (path_a, path_b):
            data = json.loads(f.read_text(encoding="utf-8"))
            assert "jobs" in data
            assert len(data["jobs"]) == 1
