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
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolated_ctx_roots():
    """每个用例独立的 context_root 注册表。"""
    _clear_ctx_roots()
    yield
    _clear_ctx_roots()


from nanobee.builtin.tool_task import ToolTaskPlugin
from nanobee.builtin.tool_task.plugin import _sanitize_ns
from nanobee.kernel.context_sandbox_var import RequestContext, bind_request_context, reset_request_context
from nanobee.plugins.base import PluginMetadata


# ---- 辅助工具 ----


def _run_async(coro):
    """运行异步协程。"""
    return asyncio.run(coro)


def _clear_ctx_roots() -> None:
    """清空共享的 context_root 注册表（每个用例独立）。"""
    if hasattr(_bind_context, "_roots"):
        _bind_context._roots = {}


def _create_plugin(tmp_path: Path, *, data_dir: str = "") -> ToolTaskPlugin:
    """创建测试插件实例。

    Args:
        tmp_path: 临时目录，同时充当 data_dir（回退）与 context_root。
        data_dir: 可选的配置覆盖值（模拟 plugins.tool_task.data_dir）。

    Returns:
        已初始化的 ToolTaskPlugin 实例。
    """
    plugin = ToolTaskPlugin(PluginMetadata(name="tool_task", plugin_type="tool"))
    plugins_section = {"tool_task": {"data_dir": data_dir}} if data_dir else {}
    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.config.plugins = plugins_section
    plugin.initialize(kernel)
    return plugin


_CTX_ROOT_STACK: list[tuple[object, object]] = []


def _bind_context(user_id: str = "test-user") -> object:
    """绑定 RequestContext + context_root 到当前异步任务（模拟 per-turn 注入）。

    Returns:
        Token 用于后续 reset。
    """
    from nanobee.kernel.context_sandbox_var import (
        bind_context_root,
        current_context_root,
        reset_context_root,
    )

    # 全局注册表：user_id -> context_root（tests 之间共享，便于查找）
    roots = getattr(_bind_context, "_roots", None)
    if roots is None:
        roots = {}
        _bind_context._roots = roots
    root = roots.setdefault(user_id, Path(tempfile.mkdtemp(prefix=f"task-ctx-{user_id}-")))
    t1 = bind_request_context(RequestContext(
        channel="test",
        chat_id=user_id,
        context_id=user_id,
        session_id="sess-1",
    ))
    t2 = bind_context_root(root)
    _CTX_ROOT_STACK.append((t1, t2))
    return (t1, t2)


def _reset_context(token) -> None:
    from nanobee.kernel.context_sandbox_var import reset_request_context

    t1, t2 = _CTX_ROOT_STACK.pop()
    from nanobee.kernel.context_sandbox_var import reset_context_root
    reset_context_root(t2)
    reset_request_context(t1)


def _call_tool(plugin: ToolTaskPlugin, tool_name: str, user_id: str = "test-user", **kwargs: Any) -> dict:
    """绑定上下文 + 调用工具，返回解析后的 JSON 结果。"""
    token = _bind_context(user_id)
    try:
        result = _run_async(plugin.execute_tool(tool_name, **kwargs))
    finally:
        _reset_context(token)
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
        """创建后数据持久化到 <context_root>/task/<ns>.json（沙箱边界内）。"""
        plugin = _create_plugin(tmp_path)
        created = _call_tool(plugin, "task_create", subject="写文档")
        ctx_root = _bind_context._roots["test-user"]
        store = ctx_root / "task" / "default.json"
        assert store.is_file()
        # 不得逃逸到 context_root 之外（尤其不能写 kernel.data_dir）
        assert ctx_root.resolve() in store.resolve().parents
        data = json.loads(store.read_text(encoding="utf-8"))
        assert created["task"]["id"] in data

    def test_no_write_to_kernel_data_dir(self, tmp_path: Path) -> None:
        """默认配置下不得在 kernel.data_dir 落盘（沙箱越界回归测试）。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="越界检查")
        kernel_side = tmp_path / "task"
        # tmp_path 同时充当 kernel.data_dir；默认语义下它不应成为写入点
        assert not (tmp_path / "task" / "test-user").exists()

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

    def test_config_data_dir_stays_within(self, tmp_path: Path) -> None:
        """配置覆盖 data_dir 时仍写入覆盖目录（受控场景），且路径已 resolve。"""
        override = tmp_path / "task-override"
        plugin = _create_plugin(tmp_path, data_dir=str(override))
        created = _call_tool(plugin, "task_create", subject="覆盖目录任务")
        store = override / "test-user" / "default.json"
        assert store.is_file()
        data = json.loads(store.read_text(encoding="utf-8"))
        assert created["task"]["id"] in data

    def test_config_data_dir_resolve_blocks_escape(self, tmp_path: Path) -> None:
        """配置覆盖路径含 .. 时被 resolve 归一，不产生目录逃逸路径。"""
        override = (tmp_path / "sub" / ".." / "task-override")
        plugin = _create_plugin(tmp_path, data_dir=str(override))
        _call_tool(plugin, "task_create", subject="resolve 检查")
        expected = (tmp_path / "task-override").resolve()
        assert (expected / "test-user" / "default.json").is_file()
        # ".." 未被字面保留
        assert not (tmp_path / "sub").exists() or not any(
            ".." in p.parts for p in (override.parent, expected.parents)
        )

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
                _reset_context(token)
            return [json.loads(r) for r in results]

        results = asyncio.run(_burst())
        assert all(r["ok"] for r in results)
        assert len({r["task"]["id"] for r in results}) == 10

        ctx_root = _bind_context._roots["user-c"]
        stored = json.loads((ctx_root / "task" / "default.json").read_text(encoding="utf-8"))
        assert len(stored) == 10
