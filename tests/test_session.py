"""
Session 管理单元测试。

覆盖：Session 数据类、SessionStore I/O、SessionManager 缓存+CRUD、
多 session 隔离、历史迁移、fork 复制、list_sessions、损坏修复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nanobee.session.session import Session
from nanobee.session.session_manager import SessionManager
from nanobee.session.session_store import SessionStore


# =============================================================================
# Session 数据类
# =============================================================================


class TestSessionDataClass:
    """Session 数据类基本功能。"""

    def test_create_session(self) -> None:
        """创建 session 时字段默认值正确。"""
        s = Session(session_id="cli:direct", user_id="user-a")
        assert s.session_id == "cli:direct"
        assert s.user_id == "user-a"
        assert s.messages == []
        assert s.metadata == {}
        assert s.last_consolidated == 0
        assert s.created_at is not None

    def test_add_message(self) -> None:
        """add_message 追加到消息列表。"""
        s = Session(session_id="s1", user_id="u1")
        s.add_message("user", "hello")
        s.add_message("assistant", "hi")
        assert len(s.messages) == 2
        assert s.messages[0] == {"role": "user", "content": "hello"}

    def test_trim_to_last_n(self) -> None:
        """trim_to_last_n 保留最近 N 条。"""
        s = Session(session_id="s1", user_id="u1")
        for i in range(10):
            s.add_message("user", str(i))
        s.trim_to_last_n(3)
        assert len(s.messages) == 3
        assert s.messages[-1]["content"] == "9"

    def test_clear(self) -> None:
        """clear 清空消息。"""
        s = Session(session_id="s1", user_id="u1")
        s.add_message("user", "hello")
        s.clear()
        assert s.messages == []

    def test_to_metadata_dict(self) -> None:
        """to_metadata_dict 输出格式正确。"""
        s = Session(session_id="s1", user_id="u1")
        s.add_message("user", "hi")
        meta = s.to_metadata_dict()
        assert meta["_type"] == "metadata"
        assert meta["session_id"] == "s1"
        assert meta["message_count"] == 1

    def test_from_metadata_dict(self) -> None:
        """from_metadata_dict 恢复骨架。"""
        data = {
            "_type": "metadata",
            "session_id": "dingtalk:conv123",
            "created_at": "2026-06-23T10:00:00",
            "updated_at": "2026-06-23T10:05:00",
            "metadata": {"title": "test"},
            "last_consolidated": 5,
            "message_count": 10,
        }
        s = Session.from_metadata_dict("user-a", data)
        assert s.session_id == "dingtalk:conv123"
        assert s.user_id == "user-a"
        assert s.metadata == {"title": "test"}
        assert s.last_consolidated == 5
        assert s.messages == []  # 消息需后续加载


# =============================================================================
# SessionStore I/O
# =============================================================================


class TestSessionStore:
    """SessionStore 文件 I/O 测试。"""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """写入后能完整读取。"""
        store = SessionStore(tmp_path / "users")
        s = Session(session_id="cli:direct", user_id="user-a")
        s.add_message("user", "hello")
        s.add_message("assistant", "world")
        store.save(s)

        loaded = store.load("user-a", "cli:direct")
        assert loaded is not None
        assert loaded.session_id == "cli:direct"
        assert loaded.user_id == "user-a"
        assert len(loaded.messages) == 2
        assert loaded.messages[0]["content"] == "hello"

    def test_save_and_load_default_session(self, tmp_path: Path) -> None:
        """默认 session_id 读写。"""
        store = SessionStore(tmp_path / "users")
        s = Session(session_id="default", user_id="user-a")
        s.add_message("user", "test")
        store.save(s)
        loaded = store.load("user-a", "default")
        assert loaded is not None
        assert loaded.messages[0]["content"] == "test"

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        """不存在的 session 返回 None。"""
        store = SessionStore(tmp_path / "users")
        assert store.load("no-such-user", "no-such-session") is None

    def test_delete(self, tmp_path: Path) -> None:
        """删除 session 文件。"""
        store = SessionStore(tmp_path / "users")
        s = Session(session_id="s1", user_id="u1")
        store.save(s)
        assert store.load("u1", "s1") is not None
        assert store.delete("u1", "s1") is True
        assert store.load("u1", "s1") is None
        assert store.delete("u1", "no-such") is False

    def test_list_sessions(self, tmp_path: Path) -> None:
        """list_sessions 返回所有 session 摘要（仅元数据行）。"""
        store = SessionStore(tmp_path / "users")
        for sid in ["a", "b", "c"]:
            s = Session(session_id=sid, user_id="u1")
            s.add_message("user", f"msg_{sid}")
            store.save(s)

        summaries = store.list_sessions("u1")
        assert len(summaries) == 3
        sids = {s["session_id"] for s in summaries}
        assert sids == {"a", "b", "c"}

    def test_atomic_write(self, tmp_path: Path) -> None:
        """原子写入不产生临时文件残留。"""
        store = SessionStore(tmp_path / "users")
        s = Session(session_id="s1", user_id="u1")
        store.save(s)

        # 检查没有临时文件
        session_dir = store._session_path("u1", "s1").parent
        tmps = [f for f in os.listdir(session_dir) if f.endswith(".tmp")]
        assert tmps == []

    def test_repair_corrupted(self, tmp_path: Path) -> None:
        """损坏的 JSONL（含无效行）能自动修复。"""
        store = SessionStore(tmp_path / "users")
        path = store._session_path("u1", "s1")

        # 写一个带损坏行的文件
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"_type":"metadata","session_id":"s1","user_id":"u1"}\n')
            f.write('{"role":"user","content":"valid"}\n')
            f.write("not-json\n")  # 损坏行
            f.write('{"role":"assistant","content":"also valid"}\n')

        loaded = store.load("u1", "s1")
        assert loaded is not None
        assert len(loaded.messages) == 2
        assert loaded.messages[0]["content"] == "valid"

    def test_repair_missing_metadata(self, tmp_path: Path) -> None:
        """首行非元数据时自动修复。"""
        store = SessionStore(tmp_path / "users")
        path = store._session_path("u1", "s1")

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"role":"user","content":"orphan"}\n')

        loaded = store.load("u1", "s1")
        assert loaded is not None
        # 修复后应包含至少那条消息
        assert len(loaded.messages) >= 1


# =============================================================================
# SessionManager（缓存 + 业务逻辑）
# =============================================================================


class TestSessionManager:
    """SessionManager 缓存和 CRUD 测试。"""

    def test_get_or_create_new(self, tmp_path: Path) -> None:
        """get_or_create 返回新的 session。"""
        mgr = SessionManager(tmp_path / "users")
        s = mgr.get_or_create("user-a", "cli:test")
        assert s.session_id == "cli:test"
        assert s.user_id == "user-a"
        assert s.messages == []

    def test_get_or_create_cached(self, tmp_path: Path) -> None:
        """get_or_create 第二次返回缓存。"""
        mgr = SessionManager(tmp_path / "users")
        s1 = mgr.get_or_create("user-a", "s1")
        s1.add_message("user", "hello")
        # 第二次获取，应该返回同一个对象
        s2 = mgr.get_or_create("user-a", "s1")
        assert s2 is s1
        assert len(s2.messages) == 1

    def test_get_or_create_from_disk(self, tmp_path: Path) -> None:
        """缓存未命中时从磁盘加载。"""
        mgr = SessionManager(tmp_path / "users")
        # 先存一个到磁盘（绕过缓存，直接使用 store）
        s = Session(session_id="disk-session", user_id="u1")
        s.add_message("user", "from disk")
        mgr.store.save(s)

        # 新 SessionManager 实例（模拟重启），应加载磁盘
        mgr2 = SessionManager(tmp_path / "users")
        loaded = mgr2.get_or_create("u1", "disk-session")
        assert loaded.messages[0]["content"] == "from disk"

    def test_save_and_delete(self, tmp_path: Path) -> None:
        """save + delete 完整生命周期。"""
        mgr = SessionManager(tmp_path / "users")
        s = mgr.get_or_create("u1", "s1")
        s.add_message("user", "msg")
        mgr.save(s)

        # 删除
        assert mgr.delete("u1", "s1") is True
        assert mgr.get_or_create("u1", "s1").messages == []  # 新 session

    def test_invalidate(self, tmp_path: Path) -> None:
        """invalidate 移除缓存。"""
        mgr = SessionManager(tmp_path / "users")
        mgr.get_or_create("u1", "s1")
        mgr.invalidate("u1", "s1")
        # 删除后应不再在缓存中（需新建）
        # 不影响磁盘持久化
        assert ("u1", "s1") not in mgr._cache

    def test_list_sessions(self, tmp_path: Path) -> None:
        """list_sessions 合并文件和缓存。"""
        mgr = SessionManager(tmp_path / "users")
        # 创建一个 session 并保存到磁盘
        s1 = mgr.get_or_create("u1", "disk-session")
        s1.add_message("user", "msg1")
        mgr.save(s1)

        # 再创建一个仅在缓存中的 session
        s2 = mgr.get_or_create("u1", "cache-only")
        s2.add_message("user", "msg2")

        summaries = mgr.list_sessions("u1")
        assert len(summaries) >= 2
        sids = {s["session_id"] for s in summaries}
        assert "disk-session" in sids
        assert "cache-only" in sids

    def test_fork(self, tmp_path: Path) -> None:
        """fork 复制 session 到新 ID。"""
        mgr = SessionManager(tmp_path / "users")
        source = mgr.get_or_create("u1", "original")
        source.add_message("user", "m1")
        source.add_message("user", "m2")
        source.add_message("assistant", "reply")
        mgr.save(source)

        # fork 全部
        forked = mgr.fork(source, "forked-all")
        assert forked is not None
        assert forked.session_id == "forked-all"
        assert len(forked.messages) == 3

        # fork 部分（前2条）
        partial = mgr.fork(source, "forked-partial", before_message_index=2)
        assert partial is not None
        assert len(partial.messages) == 2

    def test_fork_invalid_index(self, tmp_path: Path) -> None:
        """fork 越界索引返回 None。"""
        mgr = SessionManager(tmp_path / "users")
        source = mgr.get_or_create("u1", "src")
        assert mgr.fork(source, "dst", before_message_index=99) is None

    def test_flush_all(self, tmp_path: Path) -> None:
        """flush_all 将所有缓存写入磁盘。"""
        mgr = SessionManager(tmp_path / "users")
        s1 = mgr.get_or_create("u1", "s1")
        s1.add_message("user", "flush-me")
        mgr.save(s1)  # 已在磁盘

        s2 = mgr.get_or_create("u1", "s2")
        s2.add_message("user", "also-flush")
        # s2 未持久化

        flushed = mgr.flush_all()
        assert flushed >= 1

        # s2 应已在磁盘
        from_disk = mgr.store.load("u1", "s2")
        assert from_disk is not None
        assert from_disk.messages[0]["content"] == "also-flush"

    def test_default_session_backward_compat(self, tmp_path: Path) -> None:
        """默认 session_id = 'default' 保持向后兼容。"""
        mgr = SessionManager(tmp_path / "users")
        s = mgr.get_or_create("user-a")  # 不传 session_id → "default"
        assert s.session_id == "default"



# =============================================================================
# 多 session 隔离
# =============================================================================


class TestMultiSession:
    """多 session 隔离性测试。"""

    def test_independent_messages(self, tmp_path: Path) -> None:
        """同一用户的不同 session 消息独立。"""
        mgr = SessionManager(tmp_path / "users")

        s1 = mgr.get_or_create("user-a", "session-1")
        s1.add_message("user", "msg from s1")

        s2 = mgr.get_or_create("user-a", "session-2")
        s2.add_message("user", "msg from s2")

        assert len(s1.messages) == 1
        assert len(s2.messages) == 1
        assert s1.messages[0]["content"] != s2.messages[0]["content"]

    def test_different_users_same_session_id(self, tmp_path: Path) -> None:
        """不同用户相同 session_id 不冲突。"""
        mgr = SessionManager(tmp_path / "users")

        s1 = mgr.get_or_create("user-a", "default")
        s1.add_message("user", "a's msg")

        s2 = mgr.get_or_create("user-b", "default")
        s2.add_message("user", "b's msg")

        assert s1.messages[0]["content"] == "a's msg"
        assert s2.messages[0]["content"] == "b's msg"
        assert s1 is not s2

    def test_invalidate_and_reload(self, tmp_path: Path) -> None:
        """invalidate 后重新加载。"""
        mgr = SessionManager(tmp_path / "users")
        s = mgr.get_or_create("u1", "s1")
        s.add_message("user", "before-invalidate")
        mgr.save(s)

        # 直接用 store 修改文件（模拟外部写入）
        data = json.dumps(s.to_metadata_dict(), ensure_ascii=False)
        msgs = '\n'.join(json.dumps(m, ensure_ascii=False) for m in s.messages)
        mgr.store._session_path("u1", "s1").write_text(
            f"{data}\n{msgs}\n" + '\n{"role":"user","content":"external"}',
            encoding="utf-8",
        )

        # invalidate 后重新加载应看到外部写入
        mgr.invalidate("u1", "s1")
        reloaded = mgr.get_or_create("u1", "s1")
        assert len(reloaded.messages) == 2
        assert reloaded.messages[1]["content"] == "external"
