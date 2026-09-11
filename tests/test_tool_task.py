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

from nanobee.builtin.tool_task import ToolTaskPlugin
from nanobee.builtin.tool_task.plugin import _sanitize_ns
from nanobee.kernel.context_sandbox_var import (
    RequestContext,
    bind_context_root,
    bind_request_context,
    reset_context_root,
    reset_request_context,
)
from nanobee.plugins.base import PluginMetadata


@pytest.fixture(autouse=True)
def _isolated_ctx_roots():
    """每个用例独立的 context_root 注册表。"""
    _clear_ctx_roots()
    yield
    _clear_ctx_roots()


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
    """按 LIFO 恢复 _bind_context 绑定的两个 ContextVar。"""
    t1, t2 = _CTX_ROOT_STACK.pop()
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


# =============================================================================
# 评审修复回归测试
# =============================================================================


class TestReviewFixes:
    """针对评审意见的回归测试。"""

    def test_list_invalid_status_rejected(self, tmp_path: Path) -> None:
        """task_list 非法 status 显式报错，不再静默返回空清单（评审#3）。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="真实任务")

        result = _call_tool(plugin, "task_list", status="in-progress")  # 常见笔误
        assert result["ok"] is False
        assert "非法状态" in result["error"]
        # 与 task_update 的错误语义对齐
        assert "合法集合" in result["error"]

    def test_list_empty_status_still_all(self, tmp_path: Path) -> None:
        """未传 status 仍返回全部任务。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="任务A")
        result = _call_tool(plugin, "task_list")
        assert result["ok"] is True
        assert result["count"] == 1

    def test_corrupt_store_not_silently_reset(self, tmp_path: Path) -> None:
        """损坏存储文件不得静默清空并覆盖（评审#4）。"""
        plugin = _create_plugin(tmp_path)
        created = _call_tool(plugin, "task_create", subject="不可丢失的任务")
        ctx_root = _bind_context._roots["test-user"]
        store = ctx_root / "task" / "default.json"

        store.write_text("{ not valid json", encoding="utf-8")
        listed = _call_tool(plugin, "task_list")
        assert listed["ok"] is False
        assert "损坏" in listed["error"] or "非法" in listed["error"]

        # 关键：损坏期间不得触发生成新数据覆盖原文件
        created_again = _call_tool(plugin, "task_create", subject="覆盖者")
        assert created_again["ok"] is False
        assert store.read_text(encoding="utf-8") == "{ not valid json"
        # 修复文件后原任务仍在（未被覆盖丢失）
        store.write_text(json.dumps({
            created["task"]["id"]: {
                "id": created["task"]["id"],
                "subject": "不可丢失的任务",
                "description": "",
                "activeForm": "",
                "status": "pending",
            }
        }, ensure_ascii=False), encoding="utf-8")
        recovered = _call_tool(plugin, "task_get", task_id=created["task"]["id"])
        assert recovered["ok"] is True

    def test_corrupt_store_non_dict_rejected(self, tmp_path: Path) -> None:
        """非对象结构的存储文件同样报错而非当作空字典。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="任务A")
        ctx_root = _bind_context._roots["test-user"]
        (ctx_root / "task" / "default.json").write_text("[1, 2, 3]", encoding="utf-8")

        result = _call_tool(plugin, "task_list")
        assert result["ok"] is False
        assert "结构非法" in result["error"]

    def test_namespace_non_str_fallback(self, tmp_path: Path) -> None:
        """namespace 传非字符串走 str() 兜底，不抛 TypeError（评审#5）。"""
        plugin = _create_plugin(tmp_path)
        result = _call_tool(plugin, "task_create", subject="数字 ns", namespace=123)
        assert result["ok"] is True
        ctx_root = _bind_context._roots["test-user"]
        assert (ctx_root / "task" / "123.json").is_file()

    def test_save_preserves_existing_permissions(self, tmp_path: Path) -> None:
        """既有文件权限位在后续原子写中保持不变（评审#9）。"""
        import stat as _stat

        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="任务A")
        ctx_root = _bind_context._roots["test-user"]
        store = ctx_root / "task" / "default.json"
        store.chmod(0o644)
        assert _stat.S_IMODE(store.stat().st_mode) == 0o644

        _call_tool(plugin, "task_create", subject="任务B")
        assert _stat.S_IMODE(store.stat().st_mode) == 0o644

    def test_locks_bounded(self, tmp_path: Path) -> None:
        """_locks 不会随 namespace 增多无界增长（评审#7）。"""
        plugin = _create_plugin(tmp_path)
        for i in range(plugin._MAX_LOCKS + 100):
            _call_tool(plugin, "task_list", namespace=f"ns-{i}")
        assert len(plugin._locks) <= plugin._MAX_LOCKS

    def test_get_miss_hints_namespaces(self, tmp_path: Path) -> None:
        """task_get 未命中时提示可用 namespace（评审#10）。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", subject="ns 内任务", namespace="chat-a")
        result = _call_tool(plugin, "task_get", task_id="Tnonexist", namespace="chat-b")
        assert result["ok"] is False
        assert "可用 namespace" in result["error"]
        assert "chat-a" in result["error"]


class TestToolDescriptions:
    """工具 description 的触发语义契约（评审修正意见2 / A 方案）。

    触发语义必须贴在工具调用决策点旁（description），而非 core.md。
    断言只覆盖本次确立的语义，不锁死具体措辞：允许改写，不允许丢失。
    """

    @staticmethod
    def _descriptions() -> dict[str, str]:
        plugin = ToolTaskPlugin.__new__(ToolTaskPlugin)
        return {d["function"]["name"]: d["function"]["description"] for d in plugin.get_tools()}

    def test_create_has_trigger_and_anti_trigger(self) -> None:
        """task_create 需同时写明"何时调用"与"何时不要调用"。"""
        desc = self._descriptions()["task_create"]
        assert "应在以下情况调用" in desc
        assert "不要调用" in desc
        assert "多个" in desc  # 多步骤/多文件/多项要求的归纳
        assert "memory/" in desc  # 与记忆的职责边界

    def test_create_has_no_hardcoded_threshold(self) -> None:
        """不写死步骤数阈值，交由 LLM 自主判断（对齐 tool_history 的 n 值做法）。"""
        desc = self._descriptions()["task_create"]
        assert "自主判断" in desc
        assert "≥3" not in desc and ">=3" not in desc and "3 个步骤" not in desc

    def test_update_has_trigger(self) -> None:
        """task_update 需写明状态推进的调用时机。"""
        desc = self._descriptions()["task_update"]
        assert "应在以下情况调用" in desc
        assert "in_progress" in desc
        assert "completed" in desc

    def test_list_get_have_trigger(self) -> None:
        """task_list / task_get 同样需写明调用时机与彼此分工。"""
        descs = self._descriptions()
        assert "应在以下情况调用" in descs["task_list"]
        assert "汇报进度" in descs["task_list"]
        assert "应在以下情况调用" in descs["task_get"]
        assert "task_list" in descs["task_get"]

    def test_descriptions_not_in_core_template(self) -> None:
        """触发语义不落在 core_default.md（core.md 是用户管控文件，远离决策点）。"""
        core = (
            Path(__file__).resolve().parents[1] / "nanobee" / "templates" / "core_default.md"
        ).read_text(encoding="utf-8")
        for name in ("task_create", "task_update", "task_list", "task_get"):
            assert name not in core


# =============================================================================
# 二次评审修复回归测试（P0-1 / P0-2 / P1 ×2 / P2）
# =============================================================================


class TestSecondReviewFixes:
    """针对二次评审意见的回归测试。"""

    # ---- P0-1 锁回收竞态 ----

    def test_lock_identity_stable_after_reclaim(self, tmp_path: Path) -> None:
        """锁映射达到上界被回收后，同一 key 仍返回同一把锁（P0-1）。

        原实现按 ``lock.locked()`` 判定"空闲"并丢弃，而协程从取锁到
        acquire() 之间会让出控制权，此窗口内 locked() 同为 False，
        同一 namespace 会拿到两把不同的锁 → 互斥失效 → 并发丢任务。
        """
        plugin = _create_plugin(tmp_path)

        async def _scenario() -> tuple[bool, int]:
            for i in range(plugin._MAX_LOCKS - 1):
                plugin._lock(f"prefill-{i}")
            first = plugin._lock("victim")
            plugin._lock("trigger-reclaim")  # 触发回收分支
            second = plugin._lock("victim")
            return second is first, len(plugin._locks)

        same, size = asyncio.run(_scenario())
        assert same is True  # 同一 key 必须是同一把锁
        assert size <= plugin._MAX_LOCKS

    def test_locks_bounded_on_stress(self, tmp_path: Path) -> None:
        """大量不同 key 反复取锁后映射仍有界（P0-1 的泄漏面）。"""
        plugin = _create_plugin(tmp_path)

        async def _stress() -> int:
            for i in range(plugin._MAX_LOCKS * 3):
                plugin._lock(f"k-{i}")
            return len(plugin._locks)

        assert asyncio.run(_stress()) <= plugin._MAX_LOCKS

    def test_concurrent_creates_still_serialized(self, tmp_path: Path) -> None:
        """触达上界后并发创建仍不丢数据（互斥未失效）。"""
        plugin = _create_plugin(tmp_path)
        prefill = plugin._MAX_LOCKS

        async def _burst() -> list[dict]:
            token = _bind_context("user-lock")
            try:
                # 先用无关 namespace 把锁映射顶到上界，逼迫走回收分支
                for i in range(prefill):
                    await plugin.execute_tool("task_list", namespace=f"pad-{i}")
                results = await asyncio.gather(*[
                    plugin.execute_tool("task_create", subject=f"并发任务 {i}")
                    for i in range(50)
                ])
            finally:
                _reset_context(token)
            return [json.loads(r) for r in results]

        results = asyncio.run(_burst())
        assert all(r["ok"] for r in results)
        assert len({r["task"]["id"] for r in results}) == 50
        ctx_root = _bind_context._roots["user-lock"]
        stored = json.loads((ctx_root / "task" / "default.json").read_text(encoding="utf-8"))
        assert len(stored) == 50

    # ---- P0-2 超长 namespace ----

    def test_overlong_namespace_no_oserror(self, tmp_path: Path) -> None:
        """超长 namespace 不再让 OSError(ENAMETOOLONG) 穿透 execute_tool（P0-2）。"""
        plugin = _create_plugin(tmp_path)

        async def _call(ns_len: int) -> Any:
            token = _bind_context("user-long")
            try:
                return await plugin.execute_tool(
                    "task_create", subject="长 namespace", namespace="a" * ns_len
                )
            finally:
                _reset_context(token)

        result = json.loads(asyncio.run(_call(5000)))
        assert result["ok"] is True
        ctx_root = _bind_context._roots["user-long"]
        stored = list((ctx_root / "task").glob("*.json"))
        assert len(stored) == 1
        # 单段文件名不得越过 255 字节上限
        assert len(stored[0].name.encode("utf-8")) <= 255

    def test_overlong_namespace_stable_and_distinct(self, tmp_path: Path) -> None:
        """超长 namespace 截断后：同一输入稳定映射，不同输入不碰撞。"""
        plugin = _create_plugin(tmp_path)

        async def _paths() -> list[str]:
            token = _bind_context("user-long2")
            try:
                p_same_a = plugin._store_path("u", "b" * 4000)
                p_same_b = plugin._store_path("u", "b" * 4000)
                p_other = plugin._store_path("u", "b" * 3999 + "c")
            finally:
                _reset_context(token)
            return [str(p_same_a), str(p_same_b), str(p_other)]

        same_a, same_b, other = asyncio.run(_paths())
        assert same_a == same_b
        assert same_a != other  # 哈希后缀防截断碰撞

    def test_overlong_multibyte_namespace(self, tmp_path: Path) -> None:
        """多字节（中文）超长 namespace 也不越界、不抛异常。"""
        plugin = _create_plugin(tmp_path)

        async def _call() -> Any:
            token = _bind_context("user-long3")
            try:
                return await plugin.execute_tool(
                    "task_create", subject="中文长 ns", namespace="中文任务清单" * 200
                )
            finally:
                _reset_context(token)

        assert json.loads(asyncio.run(_call()))["ok"] is True
        ctx_root = _bind_context._roots["user-long3"]
        names = [p.name for p in (ctx_root / "task").glob("*.json")]
        assert len(names) == 1
        assert len(names[0].encode("utf-8")) <= 255

    # ---- P1 非 ASCII namespace ----

    def test_non_ascii_namespace_preserved(self, tmp_path: Path) -> None:
        """中文 namespace 不再静默归并为 default（P1 数据错分）。"""
        plugin = _create_plugin(tmp_path)
        assert _sanitize_ns("中文会话") == "中文会话"

        created = _call_tool(plugin, "task_create", namespace="中文会话", subject="中文清单任务")
        assert created["ok"] is True

        ctx_root = _bind_context._roots["test-user"]
        assert (ctx_root / "task" / "中文会话.json").is_file()
        # default 清单必须仍然为空，未被静默写入
        assert _call_tool(plugin, "task_list")["count"] == 0
        assert _call_tool(plugin, "task_list", namespace="中文会话")["count"] == 1

    def test_sanitize_ns_keeps_security_properties(self) -> None:
        """放行 Unicode 后仍阻断路径穿越/绝对路径/空值。"""
        assert "/" not in _sanitize_ns("a/../../b")
        assert ".." not in Path(_sanitize_ns("../../etc/passwd")).parts
        assert _sanitize_ns("/abs/path") != "/abs/path"
        assert _sanitize_ns("") == "default"
        assert _sanitize_ns("...") == "default"
        assert _sanitize_ns("a b") == "a_b"

    # ---- P1 hint 语义与代价 ----

    def test_hint_excludes_non_namespace_files(self, tmp_path: Path) -> None:
        """hint 只列真实 namespace 文件，排除 .bak / .json.json 等干扰项。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", namespace="real-ns", subject="真实清单")
        ctx_root = _bind_context._roots["test-user"]
        (ctx_root / "task" / "default.json.bak").write_text("{}", encoding="utf-8")
        (ctx_root / "task" / "odd.json.json").write_text("{}", encoding="utf-8")

        error = _call_tool(plugin, "task_get", namespace="wrong-ns", task_id="Tnone")["error"]
        assert "real-ns" in error
        assert "bak" not in error
        assert "odd.json" not in error

    def test_hint_capped(self, tmp_path: Path) -> None:
        """hint 列出的 namespace 数量有上限，避免错误串无限膨胀。"""
        plugin = _create_plugin(tmp_path)
        for i in range(plugin._MAX_HINT_NAMESPACES + 5):
            _call_tool(plugin, "task_create", namespace=f"ns-{i}", subject="x")

        error = _call_tool(plugin, "task_get", namespace="nope", task_id="Tnone")["error"]
        assert "等 10 个" in error  # 总数为 10，仅列出上限个数

    def test_empty_list_has_no_hint_noise(self, tmp_path: Path) -> None:
        """正常空清单不再附带 available_namespaces 噪音。"""
        plugin = _create_plugin(tmp_path)
        _call_tool(plugin, "task_create", namespace="other-ns", subject="x")

        result = _call_tool(plugin, "task_list", namespace="empty-ns")
        assert result["ok"] is True
        assert result["count"] == 0
        assert "available_namespaces" not in result

    # ---- 额外：symlink 越界与 IO 错误收敛 ----

    def test_task_dir_symlink_escape_blocked(self, tmp_path: Path) -> None:
        """task 目录被换成指向边界外的 symlink 时拒绝写入，不抛框架异常。"""
        plugin = _create_plugin(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        ctx_root = Path(tempfile.mkdtemp(prefix="task-ctx-escape-"))
        (ctx_root / "task").symlink_to(outside, target_is_directory=True)

        # _bind_context 会把 root 写进注册表，须在绑定时就用越界 root
        _bind_context._roots["user-escape"] = ctx_root
        token = _bind_context("user-escape")
        try:
            raw = asyncio.run(plugin.execute_tool("task_create", subject="越界写入"))
        finally:
            _reset_context(token)

        result = json.loads(raw)  # 必须是结构化结果而非抛出的异常
        assert result["ok"] is False
        assert "越界" in result["error"]
        assert not any(outside.iterdir())

    def test_save_oserror_converted_to_result(self, tmp_path: Path) -> None:
        """存储 IO 失败收敛为 {"ok": false}，不让 OSError 穿透。"""
        plugin = _create_plugin(tmp_path)

        token = _bind_context("user-io")
        try:
            import nanobee.builtin.tool_task.plugin as mod

            original = mod.ToolTaskPlugin._save

            def _boom(path: Path, tasks: dict) -> None:
                raise OSError("disk full")

            mod.ToolTaskPlugin._save = staticmethod(_boom)
            try:
                raw = asyncio.run(plugin.execute_tool("task_create", subject="写失败"))
            finally:
                mod.ToolTaskPlugin._save = original
        finally:
            _reset_context(token)

        result = json.loads(raw)
        assert result["ok"] is False
        assert "IO" not in result["error"]  # 面向 LLM 的措辞，不带内部标识
        assert "disk full" in result["error"]
