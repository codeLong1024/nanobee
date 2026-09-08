"""
Tool Task 插件测试 — 任务分解与状态跟踪工具。

覆盖：
- task_create: 正常创建、空 subject 拒绝、命名空间隔离、路径穿越防护
- task_update: 状态推进、非法状态、非法迁移、字段修改、任务不存在
- task_list: 全量列出、按状态过滤、用户隔离
- task_get: 查看详情、任务不存在
- 用户隔离：不同 context_id 数据互不可见
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.tool_task import ToolTaskPlugin
from nanobee.builtin.tool_task.plugin import _sanitize_ns
from nanobee.kernel.context_sandbox_var import RequestContext, bind_request_context, reset_request_context
from nanobee.plugins.base import PluginMetadata


# ---- 辅助工具 ----


def _run_async(coro):
    """运行异步协程。"""
    return asyncio.run(coro)


def _create_plugin(tmp_path: Path) -> ToolTaskPlugin:
    """创建测试插件实例。

    Args:
        tmp_path: 临时目录。

    Returns:
        已初始化的 ToolTaskPlugin 实例。
    """
    plugin = ToolTaskPlugin(PluginMetadata(name="tool_task", plugin_type="tool"))
    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.config.plugins = {}
    plugin.initialize(kernel)
    return plugin


def _bind_context(user_id: str = "test-user") -> object:
    """绑定 RequestContext 到当前异步任务（模拟 per-turn 上下文注入）。

    Returns:
        Token 用于后续 reset。
    """
    return bind_request_context(RequestContext(
        channel="test",
        chat_id=user_id,
        context_id=user_id,
        session_id="sess-1",
    ))


def _call_tool(plugin: ToolTaskPlugin, tool_name: str, user_id: str = "test-user", **kwargs: Any) -> dict:
    """绑定上下文 + 调用工具，返回解析后的 JSON 结果。"""
    token = _bind_context(user_id)
    try:
        result = _run_async(plugin.execute_tool(tool_name, **kwargs))
    finally:
        reset_request_context(token)
    return json.loads(result)


# =============================================================================
# task_create 测试
# =============================================================================


class TestTaskCreate:
    """task_create 工具测试。"""

    def test_create_success(self, tmp_path: Path) -> None:
        """正常创建：返回 pending 状态的任务。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_create", subject="修复登录 bug", description="认证模块异常")

        assert result["ok"] is True
        task = result["task"]
        assert task["subject"] == "修复登录 bug"
        assert task["description"] == "认证模块异常"
        assert task["status"] == "pending"
        assert task["id"].startswith("T")

    def test_create_persists_to_file(self, tmp_path: Path) -> None:
        """创建后数据持久化到 <data_dir>/<context_id>/<ns>.json。"""
        plugin = _create_plugin(tmp_path)
        created = _call_tool(plugin, "task_create", subject="写文档")
        store = tmp_path / "task" / "test-user" / "default.json"
        assert store.is_file()
        data = json.loads(store.read_text(encoding="utf-8"))
        assert created["task"]["id"] in data

    def test_create_empty_subject_rejected(self, tmp_path: Path) -> None:
        """空 subject 被拒绝。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_create", subject="   ")
        assert result["ok"] is False
        assert "subject" in result["error"]

    def test_create_namespace_isolation(self, tmp_path: Path) -> None:
        """不同命名空间数据隔离。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="ns-a 任务", namespace="chat-a")
        result_b = _call_tool(plugin, "task_list", namespace="chat-b")

        assert result_b["ok"] is True
        assert result_b["count"] == 0

    def test_create_no_context(self, tmp_path: Path) -> None:
        """无 RequestContext 时返回错误而非抛异常。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("task_create", subject="x"))
        assert "错误" in result


# =============================================================================
# task_update 测试
# =============================================================================


class TestTaskUpdate:
    """task_update 工具测试。"""

    def test_update_status_transition(self, tmp_path: Path) -> None:
        """状态推进：pending -> in_progress -> completed。"""
        plugin = _create_plugin(tmp_path)
        task_id = _call_tool(plugin, "task_create", subject="任务A")["task"]["id"]

        r1 = _call_tool(plugin, "task_update", task_id=task_id, status="in_progress")
        assert r1["task"]["status"] == "in_progress"
        r2 = _call_tool(plugin, "task_update", task_id=task_id, status="completed")
        assert r2["task"]["status"] == "completed"

    def test_update_invalid_status(self, tmp_path: Path) -> None:
        """非法状态被拒绝。"""
        plugin = _create_plugin(tmp_path)
        task_id = _call_tool(plugin, "task_create", subject="任务A")["task"]["id"]
        result = _call_tool(plugin, "task_update", task_id=task_id, status="flying")
        assert result["ok"] is False
        assert "非法状态" in result["error"]

    def test_update_illegal_transition(self, tmp_path: Path) -> None:
        """deleted 是终态，不可再迁移。"""
        plugin = _create_plugin(tmp_path)
        task_id = _call_tool(plugin, "task_create", subject="任务A")["task"]["id"]
        _call_tool(plugin, "task_update", task_id=task_id, status="deleted")
        result = _call_tool(plugin, "task_update", task_id=task_id, status="pending")
        assert result["ok"] is False
        assert "非法状态迁移" in result["error"]

    def test_update_missing_task(self, tmp_path: Path) -> None:
        """不存在的任务返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_update", task_id="Tnonexist")
        assert result["ok"] is False
        assert "不存在" in result["error"]

    def test_update_fields(self, tmp_path: Path) -> None:
        """更新 subject/description/activeForm 字段。"""
        plugin = _create_plugin(tmp_path)
        task_id = _call_tool(plugin, "task_create", subject="旧标题")["task"]["id"]
        result = _call_tool(
            plugin, "task_update",
            task_id=task_id, subject="新标题", description="新描述", activeForm="修改标题中",
        )
        assert result["task"]["subject"] == "新标题"
        assert result["task"]["description"] == "新描述"
        assert result["task"]["activeForm"] == "修改标题中"


# =============================================================================
# task_list / task_get 测试
# =============================================================================


class TestTaskListAndGet:
    """task_list 与 task_get 工具测试。"""

    def test_list_all_and_filter(self, tmp_path: Path) -> None:
        """列出全部任务并支持按状态过滤。"""
        plugin = _create_plugin(tmp_path)
        id_a = _call_tool(plugin, "task_create", subject="任务A")["task"]["id"]
        _call_tool(plugin, "task_create", subject="任务B")
        _call_tool(plugin, "task_update", task_id=id_a, status="in_progress")

        all_tasks = _call_tool(plugin, "task_list")
        assert all_tasks["count"] == 2

        filtered = _call_tool(plugin, "task_list", status="in_progress")
        assert filtered["count"] == 1
        assert filtered["tasks"][0]["subject"] == "任务A"

    def test_list_empty(self, tmp_path: Path) -> None:
        """空清单返回 count=0。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_list")
        assert result["ok"] is True
        assert result["count"] == 0

    def test_get_existing(self, tmp_path: Path) -> None:
        """查看存在的任务。"""
        plugin = _create_plugin(tmp_path)
        task_id = _call_tool(plugin, "task_create", subject="任务A", activeForm="处理A中")["task"]["id"]
        result = _call_tool(plugin, "task_get", task_id=task_id)
        assert result["ok"] is True
        assert result["task"]["activeForm"] == "处理A中"

    def test_get_missing(self, tmp_path: Path) -> None:
        """查看不存在的任务返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_get", task_id="Tnonexist")
        assert result["ok"] is False
        assert "不存在" in result["error"]


# =============================================================================
# 用户隔离与安全测试
# =============================================================================


class TestIsolationAndSecurity:
    """多用户隔离与路径安全测试。"""

    def test_users_isolated(self, tmp_path: Path) -> None:
        """用户 A 的任务对用户 B 不可见。"""
        plugin = _create_plugin(tmp_path)
        created = _call_tool(plugin, "task_create", user_id="user-a", subject="A 的任务")

        result_b = _call_tool(plugin, "task_get", user_id="user-b", task_id=created["task"]["id"])
        assert result_b["ok"] is False

        list_b = _call_tool(plugin, "task_list", user_id="user-b")
        assert list_b["count"] == 0

    def test_sanitize_ns_traversal(self) -> None:
        """namespace 净化阻断路径穿越与特殊字符。"""
        sanitized = _sanitize_ns("../../etc/passwd")
        assert ".." not in Path(sanitized).parts
        assert "/" not in sanitized
        assert _sanitize_ns("/absolute") != "/absolute"
        assert _sanitize_ns("") == "default"
        assert _sanitize_ns("a b/c") == "a_b_c"

    def test_unknown_tool_raises(self, tmp_path: Path) -> None:
        """未知工具名抛 ValueError。"""
        plugin = _create_plugin(tmp_path)
        with pytest.raises(ValueError):
            _run_async(plugin.execute_tool("no_such_tool"))

    def test_concurrent_creates_all_persisted(self, tmp_path: Path) -> None:
        """并发创建不丢数据（per-namespace asyncio.Lock）。"""
        plugin = _create_plugin(tmp_path)

        async def _burst() -> list[dict]:
            token = _bind_context("user-c")
            try:
                results = await asyncio.gather(*[
                    plugin.execute_tool("task_create", subject=f"并发任务 {i}")
                    for i in range(10)
                ])
            finally:
                reset_request_context(token)
            return [json.loads(r) for r in results]

        results = asyncio.run(_burst())
        assert all(r["ok"] for r in results)
        assert len({r["task"]["id"] for r in results}) == 10

        stored = json.loads((tmp_path / "task" / "user-c" / "default.json").read_text(encoding="utf-8"))
        assert len(stored) == 10
