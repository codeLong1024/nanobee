"""Session 协议消息落盘入口（add_protocol_message）测试。

对应《会话工具轨迹持久化》方案 §5.1：会话侧只保留一条协议消息写入路径，
非法 role / 缺协议键当场 raise（不静默写坏历史），浅拷贝隔离调用方后续改动，
协议键在 JSONL 序列化/加载往返后保持原样。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.session.session import Session
from nanobee.session.session_manager import SessionManager


def _assistant_call(call_id: str = "call_1") -> dict:
    """构造一条 assistant(tool_calls) 协议消息。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "cron_create", "arguments": '{"cron": "* * * * *"}'},
            },
        ],
    }


def _tool_result(call_id: str = "call_1") -> dict:
    """构造一条 tool(result) 协议消息。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "cron_create",
        "content": "task_id=997b042a-bf5",
    }


class TestAddProtocolMessageValidation:
    """入口校验：非法写入必须当场暴露。"""

    def test_non_dict_rejected(self) -> None:
        session = Session(session_id="s", user_id="u")
        with pytest.raises(TypeError, match="必须是 dict"):
            session.add_protocol_message(["not", "a", "dict"])  # type: ignore[arg-type]
        assert session.messages == []

    def test_plain_text_role_rejected(self) -> None:
        """普通文本角色（user/system）不属于协议消息，必须被拒绝。"""
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="role 非法"):
            session.add_protocol_message({"role": "user", "content": "hi"})

    def test_assistant_without_tool_calls_rejected(self) -> None:
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="tool_calls"):
            session.add_protocol_message({"role": "assistant", "content": "纯文本"})

    def test_assistant_with_empty_tool_calls_rejected(self) -> None:
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="tool_calls"):
            session.add_protocol_message(
                {"role": "assistant", "content": None, "tool_calls": []},
            )

    def test_tool_without_tool_call_id_rejected(self) -> None:
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="tool_call_id"):
            session.add_protocol_message({"role": "tool", "content": "ok"})

    def test_tool_with_empty_tool_call_id_rejected(self) -> None:
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="tool_call_id"):
            session.add_protocol_message({"role": "tool", "content": "ok", "tool_call_id": ""})

    def test_rejected_write_leaves_session_untouched(self) -> None:
        session = Session(session_id="s", user_id="u")
        session.add_message("user", "hi")
        with pytest.raises(ValueError):
            session.add_protocol_message({"role": "assistant", "content": "x"})
        assert session.messages == [{"role": "user", "content": "hi"}]

    def test_add_message_rejects_tool_role(self) -> None:
        """第二条入口必须关死：缺 tool_call_id 的工具结果不能从 add_message 溜进历史。"""
        session = Session(session_id="s", user_id="u")
        with pytest.raises(ValueError, match="add_protocol_message"):
            session.add_message("tool", "ok")

        assert session.messages == []


class TestAddProtocolMessagePersistence:
    """合法写入：协议键原样保留 + 浅拷贝隔离。"""

    def test_assistant_tool_calls_appended_with_protocol_keys(self) -> None:
        session = Session(session_id="s", user_id="u")
        session.add_protocol_message(_assistant_call("call_7"))

        assert len(session.messages) == 1
        entry = session.messages[0]
        assert entry["role"] == "assistant"
        assert entry["tool_calls"][0]["id"] == "call_7"
        assert entry["tool_calls"][0]["function"]["name"] == "cron_create"

    def test_tool_result_appended_with_tool_call_id(self) -> None:
        session = Session(session_id="s", user_id="u")
        session.add_protocol_message(_tool_result("call_7"))

        assert session.messages[0]["tool_call_id"] == "call_7"
        assert session.messages[0]["name"] == "cron_create"

    def test_top_level_key_replacement_does_not_leak(self) -> None:
        """调用方事后替换顶层键（runner 合并注入的写法）不得回写已落盘历史。"""
        message = _assistant_call("call_1")
        session = Session(session_id="s", user_id="u")
        session.add_protocol_message(message)

        message["content"] = "被合并进来的注入内容"
        assert session.messages[0]["content"] is None

    def test_tool_calls_list_copy_isolates_caller_mutation(self) -> None:
        """tool_calls 列表另拷一层：调用方 append 不回写。"""
        message = _assistant_call("call_1")
        session = Session(session_id="s", user_id="u")
        session.add_protocol_message(message)

        message["tool_calls"].append({"id": "call_2", "function": {"name": "x"}})
        assert len(session.messages[0]["tool_calls"]) == 1

    def test_updated_at_refreshed(self) -> None:
        session = Session(session_id="s", user_id="u")
        before = session.updated_at
        session.add_protocol_message(_tool_result())
        assert session.updated_at >= before


class TestProtocolMessageJsonlRoundtrip:
    """协议键必须能穿过 SessionStore 的 JSONL 序列化/加载往返（方案 R2）。"""

    def test_protocol_keys_survive_roundtrip(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("u1", "test:c1")
        session.add_message("user", "建个定时任务")
        session.add_protocol_message(_assistant_call("call_1"))
        session.add_protocol_message(_tool_result("call_1"))
        session.add_message("assistant", "已创建")
        manager.save(session)

        # 绕开缓存重新从磁盘加载，验证落盘形态
        loaded = manager.store.load("u1", "test:c1")
        assert loaded is not None
        assert loaded.messages == session.messages

        protocol_entry = loaded.messages[1]
        assert protocol_entry["tool_calls"][0]["id"] == "call_1"
        assert loaded.messages[2]["tool_call_id"] == "call_1"

    def test_legacy_session_without_protocol_rows_still_loads(self, tmp_path: Path) -> None:
        """旧会话文件（无协议行）天然兼容：加载路径不需要任何迁移。"""
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("u1", "test:c1")
        session.add_message("user", "hi")
        session.add_message("assistant", "hello")
        manager.save(session)

        loaded = manager.store.load("u1", "test:c1")
        assert loaded is not None
        assert loaded.messages == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
