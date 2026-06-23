"""
Tool History 插件测试 — trim_history 和 consolidate_history 工具。

覆盖：
- trim_history: 正常裁剪、无效参数、无需裁剪、缺少上下文
- consolidate_history: 正常压缩、空摘要拒绝、无效参数、不需要压缩、
  归档文件创建、system 消息注入、last_consolidated 更新
- SessionStore append_consolidation: 新建文件、追加已有文件
- SessionManager consolidate: 完整流程、raw archive 降级
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.tool_history import ToolHistoryPlugin
from nanobee.session.session_manager import SessionManager
from nanobee.session.session_store import SessionStore


# ---- 辅助工具 ----


def _run_async(coro):
    """运行异步协程。"""
    return asyncio.run(coro)


def _create_plugin(tmp_path: Path, user_id: str = "test-user", session_id: str = "test-session") -> ToolHistoryPlugin:
    """创建带 SessionManager 的测试插件。

    Args:
        tmp_path: 临时目录。
        user_id: 测试用户 ID。
        session_id: 测试会话 ID。

    Returns:
        已初始化的 ToolHistoryPlugin 实例。
    """
    plugin = ToolHistoryPlugin()

    # 创建真实的 SessionManager
    session_manager = SessionManager(tmp_path / "users")

    # 模拟 kernel
    kernel = MagicMock()
    kernel.session_manager = session_manager

    plugin.initialize(kernel)
    plugin.set_context(user_id=user_id, session_key=session_id)

    # 预填充一些消息
    session = session_manager.get_or_create(user_id, session_id)
    for i in range(20):
        session.add_message("user" if i % 2 == 0 else "assistant", f"message {i}")
    session_manager.save(session)

    return plugin


# =============================================================================
# trim_history 测试
# =============================================================================


class TestTrimHistory:
    """trim_history 工具测试。"""

    def test_trim_history_success(self, tmp_path: Path) -> None:
        """正常裁剪：20 条消息 → 保留 5 条。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("trim_history", n=5))
        assert "20 → 5" in result or "裁剪完成" in result

        session = plugin.kernel.session_manager.get_or_create("test-user", "test-session")
        assert len(session.messages) == 5

    def test_trim_history_invalid_n_too_small(self, tmp_path: Path) -> None:
        """n < 2 时返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("trim_history", n=1))
        assert "错误" in result or "≥2" in result

    def test_trim_history_invalid_n_type(self, tmp_path: Path) -> None:
        """n 为非整数时返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("trim_history", n="abc"))
        assert "错误" in result or "≥2" in result

    def test_trim_history_no_need(self, tmp_path: Path) -> None:
        """消息数未超过 n 时无需裁剪。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("trim_history", n=100))
        assert "无需裁剪" in result or "未超过" in result

    def test_trim_history_no_user_context(self, tmp_path: Path) -> None:
        """缺少用户上下文时返回错误。"""
        plugin = ToolHistoryPlugin()
        kernel = MagicMock()
        kernel.session_manager = SessionManager(tmp_path / "users")
        plugin.initialize(kernel)
        # 未调用 set_context，_user_id 为空

        result = _run_async(plugin.execute_tool("trim_history", n=5))
        assert "错误" in result or "无法获取" in result

    def test_trim_history_no_session_manager(self) -> None:
        """无 session_manager 时返回错误。"""
        plugin = ToolHistoryPlugin()
        kernel = MagicMock()
        kernel.session_manager = None
        plugin.initialize(kernel)
        plugin.set_context(user_id="test-user", session_key="test-session")

        result = _run_async(plugin.execute_tool("trim_history", n=5))
        assert "错误" in result or "不可用" in result

    def test_trim_history_unknown_tool(self, tmp_path: Path) -> None:
        """未知工具名抛出 ValueError。"""
        plugin = _create_plugin(tmp_path)
        with pytest.raises(ValueError, match="未知工具"):
            _run_async(plugin.execute_tool("nonexistent_tool"))


# =============================================================================
# consolidate_history 测试
# =============================================================================


class TestConsolidateHistory:
    """consolidate_history 工具测试。"""

    def test_consolidate_history_success(self, tmp_path: Path) -> None:
        """正常压缩：20 条 → 保留 5 条，归档 15 条。"""
        plugin = _create_plugin(tmp_path)
        summary = "用户讨论了消息测试，关键决策：保留5条最近消息。"
        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary=summary, keep_last_n=5,
        ))
        assert "压缩完成" in result
        assert "15" in result  # archived_count = 20 - 5

        session = plugin.kernel.session_manager.get_or_create("test-user", "test-session")
        # 5 条裁剪后 + 1 条 system 摘要消息 = 6 条
        assert len(session.messages) == 6
        assert session.messages[0]["role"] == "system"
        assert summary in session.messages[0]["content"]
        assert session.last_consolidated == 15

    def test_consolidate_history_empty_summary(self, tmp_path: Path) -> None:
        """空摘要应返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="", keep_last_n=5,
        ))
        assert "错误" in result
        assert "不能为空" in result

    def test_consolidate_history_whitespace_summary(self, tmp_path: Path) -> None:
        """全空白摘要应返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="   ", keep_last_n=5,
        ))
        assert "错误" in result

    def test_consolidate_history_invalid_keep_last_n(self, tmp_path: Path) -> None:
        """keep_last_n < 2 时返回错误。"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="摘要文本", keep_last_n=1,
        ))
        assert "错误" in result or "≥2" in result

    def test_consolidate_history_default_keep_last_n(self, tmp_path: Path) -> None:
        """不传 keep_last_n 时使用默认值 8。"""
        plugin = _create_plugin(tmp_path)
        summary = "默认保留 8 条的压缩测试。"
        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary=summary,
        ))
        assert "压缩完成" in result
        # 20 - 默认 8 = 12 条归档
        assert "12" in result

        session = plugin.kernel.session_manager.get_or_create("test-user", "test-session")
        # 8 条保留 + 1 条 system = 9 条
        assert len(session.messages) == 9

    def test_consolidate_history_no_need(self, tmp_path: Path) -> None:
        """消息数未超过 keep_last_n 时无需压缩。"""
        # 创建仅有 3 条消息的 session
        plugin = ToolHistoryPlugin()
        session_manager = SessionManager(tmp_path / "users")
        kernel = MagicMock()
        kernel.session_manager = session_manager
        plugin.initialize(kernel)
        plugin.set_context(user_id="small-user", session_key="small-session")

        session = session_manager.get_or_create("small-user", "small-session")
        for i in range(3):
            session.add_message("user", f"msg {i}")
        session_manager.save(session)

        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="摘要文本", keep_last_n=10,
        ))
        assert "无需压缩" in result or "未超过" in result

    def test_consolidate_history_no_user_context(self, tmp_path: Path) -> None:
        """缺少用户上下文时返回错误。"""
        plugin = ToolHistoryPlugin()
        kernel = MagicMock()
        kernel.session_manager = SessionManager(tmp_path / "users")
        plugin.initialize(kernel)
        # 未调用 set_context

        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="摘要文本",
        ))
        assert "错误" in result or "无法获取" in result

    def test_consolidate_history_no_session_manager(self) -> None:
        """无 session_manager 时返回错误。"""
        plugin = ToolHistoryPlugin()
        kernel = MagicMock()
        kernel.session_manager = None
        plugin.initialize(kernel)
        plugin.set_context(user_id="test-user", session_key="test-session")

        result = _run_async(plugin.execute_tool(
            "consolidate_history", summary="摘要文本",
        ))
        assert "错误" in result or "不可用" in result

    def test_consolidate_history_creates_archive(self, tmp_path: Path) -> None:
        """consolidate_history 应创建 .consolidation.jsonl 归档文件。"""
        plugin = _create_plugin(tmp_path)
        summary = "验证归档文件创建的压缩测试。"
        _run_async(plugin.execute_tool(
            "consolidate_history", summary=summary, keep_last_n=5,
        ))

        # 检查归档文件
        store = SessionStore(tmp_path / "users")
        archive_path = store._consolidation_path("test-user", "test-session")
        assert archive_path.exists()
        lines = archive_path.read_text("utf-8").strip().split("\n")
        assert len(lines) == 1
        record = __import__("json").loads(lines[0])
        assert record["summary"] == summary
        assert record["archived_count"] == 15
        assert record["index"] == 0
        assert "timestamp" in record

    def test_consolidate_history_multiple_appends(self, tmp_path: Path) -> None:
        """多次 consolidate 应追加而非覆盖归档。"""
        plugin = _create_plugin(tmp_path)
        # 第一次压缩
        _run_async(plugin.execute_tool(
            "consolidate_history", summary="第一次压缩。", keep_last_n=10,
        ))
        # 第二次压缩（在压缩后的 11 条消息上再压缩）
        _run_async(plugin.execute_tool(
            "consolidate_history", summary="第二次压缩。", keep_last_n=3,
        ))

        store = SessionStore(tmp_path / "users")
        archive_path = store._consolidation_path("test-user", "test-session")
        lines = archive_path.read_text("utf-8").strip().split("\n")
        assert len(lines) == 2
        record0 = __import__("json").loads(lines[0])
        record1 = __import__("json").loads(lines[1])
        assert record0["index"] == 0
        assert record1["index"] == 1
        assert "第一次压缩" in record0["summary"]
        assert "第二次压缩" in record1["summary"]

    def test_consolidate_history_system_message_content(self, tmp_path: Path) -> None:
        """system 消息应包含摘要和元信息。"""
        plugin = _create_plugin(tmp_path)
        summary = "关键决策：选择方案A，用户偏好：Python语言。"
        _run_async(plugin.execute_tool(
            "consolidate_history", summary=summary, keep_last_n=8,
        ))

        session = plugin.kernel.session_manager.get_or_create("test-user", "test-session")
        system_msg = session.messages[0]
        assert system_msg["role"] == "system"
        assert "历史摘要" in system_msg["content"]
        assert summary in system_msg["content"]
        assert "12 条消息已归档" in system_msg["content"]


# =============================================================================
# SessionStore append_consolidation 测试
# =============================================================================


class TestAppendConsolidation:
    """SessionStore.append_consolidation 单元测试。"""

    def test_append_creates_file(self, tmp_path: Path) -> None:
        """append_consolidation 应创建新文件。"""
        store = SessionStore(tmp_path / "users")
        store.append_consolidation("u1", "s1", "摘要内容", 10)

        path = store._consolidation_path("u1", "s1")
        assert path.exists()

    def test_append_increments_index(self, tmp_path: Path) -> None:
        """多次追加应递增 index。"""
        store = SessionStore(tmp_path / "users")
        idx0 = store.append_consolidation("u1", "s1", "第一次", 5)
        idx1 = store.append_consolidation("u1", "s1", "第二次", 3)
        assert idx0 == 0
        assert idx1 == 1

        path = store._consolidation_path("u1", "s1")
        lines = path.read_text("utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_append_record_format(self, tmp_path: Path) -> None:
        """追加记录格式正确。"""
        store = SessionStore(tmp_path / "users")
        store.append_consolidation("u1", "s1", "摘要", 42)

        path = store._consolidation_path("u1", "s1")
        record = __import__("json").loads(path.read_text("utf-8").strip())
        assert record["index"] == 0
        assert record["summary"] == "摘要"
        assert record["archived_count"] == 42
        assert "timestamp" in record


# =============================================================================
# SessionManager consolidate 完整流程
# =============================================================================


class TestSessionManagerConsolidate:
    """SessionManager.consolidate 完整流程测试。"""

    def test_consolidate_full_flow(self, tmp_path: Path) -> None:
        """完整流程：归档 → 裁剪 → system 注入 → last_consolidated。"""
        mgr = SessionManager(tmp_path / "users")
        session = mgr.get_or_create("u1", "s1")
        for i in range(15):
            session.add_message("user" if i % 2 == 0 else "assistant", str(i))
        mgr.save(session)

        result = mgr.consolidate("u1", "s1", "完整流程测试摘要", keep_last_n=5)

        assert result["archived_count"] == 10
        assert result["archived_index"] == 0
        assert result["before_count"] == 15
        assert result["after_count"] == 6  # 5 retained + 1 system

        # 重新加载验证
        reloaded = mgr.get_or_create("u1", "s1")  # 从缓存返回
        assert len(reloaded.messages) == 6
        assert reloaded.messages[0]["role"] == "system"
        assert "完整流程测试摘要" in reloaded.messages[0]["content"]
        assert reloaded.last_consolidated == 10

    def test_consolidate_different_users_independent(self, tmp_path: Path) -> None:
        """不同用户的 consolidate 应独立。"""
        mgr = SessionManager(tmp_path / "users")

        for uid in ["u1", "u2"]:
            session = mgr.get_or_create(uid, "s1")
            for i in range(10):
                session.add_message("user", f"{uid}-msg-{i}")
            mgr.save(session)

        # 各自压缩
        r1 = mgr.consolidate("u1", "s1", "u1 摘要", keep_last_n=3)
        r2 = mgr.consolidate("u2", "s1", "u2 摘要", keep_last_n=4)

        assert r1["archived_count"] == 7  # 10 - 3
        assert r2["archived_count"] == 6  # 10 - 4

        s1 = mgr.get_or_create("u1", "s1")
        s2 = mgr.get_or_create("u2", "s1")
        assert "u1 摘要" in s1.messages[0]["content"]
        assert "u2 摘要" in s2.messages[0]["content"]

    def test_consolidate_empty_summary_raw_archive(self, tmp_path: Path) -> None:
        """空摘要时降级为 raw archive。"""
        mgr = SessionManager(tmp_path / "users")
        session = mgr.get_or_create("u1", "s1")
        for i in range(10):
            session.add_message("user", f"msg {i}")
        mgr.save(session)

        result = mgr.consolidate("u1", "s1", summary="", keep_last_n=3)
        assert result["archived_count"] == 7

        # system 消息应包含 [raw archive] 标记
        reloaded = mgr.get_or_create("u1", "s1")
        assert "[raw archive]" in reloaded.messages[0]["content"]

    def test_consolidate_invalid_keep_last_n(self, tmp_path: Path) -> None:
        """keep_last_n 无效时抛出 ValueError。"""
        mgr = SessionManager(tmp_path / "users")
        session = mgr.get_or_create("u1", "s1")
        session.add_message("user", "hello")
        mgr.save(session)

        with pytest.raises(ValueError, match="≥ 1"):
            mgr.consolidate("u1", "s1", "摘要", keep_last_n=0)
