"""MemoryPlugin 接口 - 记忆插件"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any

from nanobee.plugins.base import NanobeePlugin

logger = logging.getLogger(__name__)


class MemoryPlugin(NanobeePlugin):
    """记忆插件基类

    记忆插件负责 Agent 的长期记忆存储和检索。
    """

    plugin_type = "memory"

    @abstractmethod
    async def store(self, key: str, value: Any, memory_type: str = "fact") -> None:
        """存储记忆

        Args:
            key: 记忆键（唯一标识）
            value: 记忆内容
            memory_type: 记忆类型（fact | episodic | working）
        """
        ...

    @abstractmethod
    async def retrieve(self, key: str) -> Any | None:
        """检索记忆

        Args:
            key: 记忆键

        Returns:
            记忆内容，不存在则返回 None
        """
        ...

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """搜索相关记忆

        Args:
            query: 搜索查询
            limit: 返回结果数量上限

        Returns:
            记忆列表，每个元素包含 key, value, score
        """
        ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """删除记忆

        Args:
            key: 记忆键

        Returns:
            是否删除成功
        """
        ...

    @abstractmethod
    async def list_all(self, memory_type: str | None = None) -> list[str]:
        """列出所有记忆键

        Args:
            memory_type: 按类型过滤，None 表示全部

        Returns:
            记忆键列表
        """
        ...
