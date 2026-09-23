"""
Session — 用户下的独立对话会话。

每个 Session 代表一个独立的对话，拥有独立的历史消息列表和元数据。
Session 在 UserContext 之下，不改变沙箱隔离边界、插件系统、技能管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Session:
    """一个独立的对话会话。

    Attributes:
        session_id: 会话唯一标识（格式 channel:chat_id）。
        user_id: 所属用户 ID。
        messages: 对话消息列表，每条为 {"role": str, "content": str}。
        created_at: 会话创建时间。
        updated_at: 最后更新时间。
        metadata: 会话级元数据（标题、goal_state 等）。
        last_consolidated: 已归档的消息累计条数。
    """

    session_id: str
    user_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

    # 协议消息角色白名单（机制常量）：只承载 OpenAI 兼容协议的这两类消息。
    _PROTOCOL_ROLES = frozenset({"assistant", "tool"})

    def _touch_updated_at(self) -> None:
        """刷新会话更新时间（所有消息写入路径的唯一时间戳触点）。"""
        self.updated_at = datetime.now()

    def add_message(self, role: str, content: str) -> None:
        """添加一条消息到会话末尾。

        Args:
            role: 角色（user / assistant / system）。
            content: 消息文本。

        Raises:
            ValueError: role 为 ``tool``。工具结果必须携带 ``tool_call_id``，
                只能走 :meth:`add_protocol_message`；在此拦住可避免"缺 id 的
                工具结果"这条非法历史从第二条入口溜进会话文件。
        """
        if role == "tool":
            raise ValueError("工具结果必须走 add_protocol_message（需 tool_call_id）")
        self.messages.append({"role": role, "content": content})
        self._touch_updated_at()

    def add_protocol_message(self, message: dict[str, Any]) -> None:
        """追加一条协议消息（assistant(tool_calls) / tool(result)）到会话末尾。

        与 :meth:`add_message` 的分工：本方法接受完整消息 dict（含 ``tool_calls`` /
        ``tool_call_id`` / ``name`` 等协议键），浅拷贝后原样落盘——内容清洗（截断、
        思维链剥离）与脱敏是调用方（AgentLoop 保存阶段）的职责。本方法是
        assistant(tool_calls) / tool(result) 这两类协议消息的**唯一入口**（
        :meth:`add_message` 已拒绝 ``role="tool"``），把"写坏历史"挡在写入时刻。

        Args:
            message: 含 ``role`` 与协议键的消息 dict。

        Raises:
            TypeError: 入参不是 dict。
            ValueError: role 不在 {assistant, tool}；assistant 缺非空 ``tool_calls``；
                tool 缺非空 ``tool_call_id``。非法写入必须当场暴露，不静默产出一个
                后续回放必炸的历史。
        """
        if not isinstance(message, dict):
            raise TypeError(f"协议消息必须是 dict，收到 {type(message).__name__}")
        role = message.get("role")
        if role not in self._PROTOCOL_ROLES:
            raise ValueError(f"协议消息 role 非法：{role!r}（仅允许 assistant/tool）")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                raise ValueError("assistant 协议消息必须含非空 tool_calls 列表")
        else:
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise ValueError("tool 协议消息必须含非空 tool_call_id")
        # 浅拷贝顶层 dict；tool_calls 列表再拷一层，防调用方后续 append/替换
        # 串写到已落盘历史（嵌套 function dict 由调用方移交所有权）
        entry = dict(message)
        if isinstance(entry.get("tool_calls"), list):
            entry["tool_calls"] = [
                dict(call) if isinstance(call, dict) else call
                for call in entry["tool_calls"]
            ]
        self.messages.append(entry)
        self._touch_updated_at()

    def trim_to_last_n(self, n: int) -> None:
        """裁剪历史，仅保留最近 n 条消息。

        Args:
            n: 保留的最新消息条数。n <= 0 时清空。
        """
        if n <= 0:
            self.messages.clear()
        elif len(self.messages) > n:
            self.messages = self.messages[-n:]
        self._touch_updated_at()

    def clear(self) -> None:
        """清空会话消息。"""
        self.messages.clear()
        self._touch_updated_at()

    def to_metadata_dict(self) -> dict[str, Any]:
        """生成首行元数据字典。"""
        return {
            "_type": "metadata",
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
            "last_consolidated": self.last_consolidated,
            "message_count": len(self.messages),
        }

    @classmethod
    def from_metadata_dict(cls, user_id: str, data: dict[str, Any]) -> Session:
        """从元数据字典恢复 Session（仅创建骨架，messages 需后续加载）。

        Args:
            user_id: 用户 ID。
            data: 元数据字典（来自 JSONL 首行）。

        Returns:
            Session 实例（含框架数据，不含 messages）。
        """
        created_at = datetime.now()
        if raw := data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        updated_at = created_at
        if raw := data.get("updated_at"):
            try:
                updated_at = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return cls(
            session_id=str(data.get("session_id", "")),
            user_id=user_id,
            created_at=created_at,
            updated_at=updated_at,
            metadata=dict(data.get("metadata", {})),
            last_consolidated=int(data.get("last_consolidated", 0)),
        )


__all__ = [
    "Session",
]
