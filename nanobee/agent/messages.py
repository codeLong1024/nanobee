"""消息数据模型 — 入站和出站消息定义。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """来自聊天通道的消息。"""

    channel: str
    sender_id: str
    chat_id: str
    content: str
    timestamp: Any = field(default_factory=time.time)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    context_id_override: str | None = None
    session_id_override: str | None = None
    # 声明式机制标记：为 True 时本次 turn 不加载该用户历史，
    # 改用独立隔离空会话（一次性，turn 结束即回收）。典型场景如
    # cron 定时触发等无上下文的任务；调用方按需自行声明，框架只读
    # 此标记决定是否加载历史，不关心"为何声明"（框架无知论）。
    fresh_session: bool = False

    @property
    def context_id(self) -> str:
        """获取用户上下文隔离键（决定 UserContext 目录归属）。

        注意：此属性与 ``handle_message`` 的 ``context_id`` 参数同名但不同义——
        参数只落到 ``chat_id`` 槽位，不参与隔离；真正的隔离键是本属性。

        优先级:
        1. context_id_override 显式指定
        2. sender_id (用户唯一标识,如钉钉 staffId)
        3. channel:chat_id (兜底兼容)

        参考 nanobot_channel_dingtalk 的设计,使用 sender_id 作为唯一标识,
        避免创建重复的用户目录。
        """
        if self.context_id_override:
            return self.context_id_override
        if self.sender_id:
            return self.sender_id
        return f"{self.channel}:{self.chat_id}"

    @property
    def session_id(self) -> str:
        """获取会话 ID。

        优先级:
        1. session_id_override 显式指定
        2. channel:chat_id（chat_id 非 "direct" 时）
        3. "default"（兜底）

        Session 在 UserContext 之下，一个用户可有多个独立会话。
        """
        if self.session_id_override:
            return self.session_id_override
        if self.chat_id and self.chat_id != "direct":
            return f"{self.channel}:{self.chat_id}"
        return "default"


@dataclass
class OutboundMessage:
    """发送到聊天通道的消息。"""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
