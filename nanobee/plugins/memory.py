"""MemoryPlugin 接口 - 记忆管理底座"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.logger import logger



class MemoryPlugin(NanobeePlugin):
    """记忆管理底座接口——框架只调用两个方法。

    框架在 BUILD 状态调用 retrieve() 检索相关记忆注入 System Prompt。
    store() 由 LLM 通过记忆相关工具自主调用（框架不持有存储时机策略）。

    业务策略（什么时候存、存什么、怎么查）由插件的具体实现决定。
    """

    plugin_type = "memory"

    @abstractmethod
    async def store(self, messages: list[dict[str, Any]], user_context: Any) -> None:
        """存储/提取记忆（由 LLM 通过工具自主触发，框架不主动调用）。

        Args:
            messages: 当前完整的历史消息列表（含本轮）
            user_context: 当前用户上下文（UserContext 实例）
        """
        ...

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        user_context: Any,
        top_k: int = 5,
    ) -> str | None:
        """检索相关记忆（由 BUILD 状态触发）。

        Args:
            query: 检索查询（通常为用户当前消息）
            user_context: 当前用户上下文（UserContext 实例）
            top_k: 返回结果数量上限

        Returns:
            格式化的记忆文本，由框架注入 System Prompt
        """
        ...
