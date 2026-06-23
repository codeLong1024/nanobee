"""
Session — 用户下的独立对话会话。

每个 Session 代表一个独立的对话，拥有独立的历史消息列表和元数据。
Session 在 UserContext 之下，不改变沙箱隔离边界、插件系统、技能管理。
"""

from __future__ import annotations

import json
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
        last_consolidated: 已归档到的消息索引（为未来 Consolidator 预留）。
    """

    session_id: str
    user_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

    def add_message(self, role: str, content: str) -> None:
        """添加一条消息到会话末尾。

        Args:
            role: 角色（user / assistant / system）。
            content: 消息文本。
        """
        self.messages.append({"role": role, "content": content})
        self.updated_at = datetime.now()

    def trim_to_last_n(self, n: int) -> None:
        """裁剪历史，仅保留最近 n 条消息。

        Args:
            n: 保留的最新消息条数。n <= 0 时清空。
        """
        if n <= 0:
            self.messages.clear()
        elif len(self.messages) > n:
            self.messages = self.messages[-n:]
        self.updated_at = datetime.now()

    def clear(self) -> None:
        """清空会话消息。"""
        self.messages.clear()
        self.updated_at = datetime.now()

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
