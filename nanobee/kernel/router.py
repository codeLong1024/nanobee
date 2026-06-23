"""
上下文路由器 — 将 channel 消息路由到正确的用户上下文

路由策略（可扩展）：
1. 如果 InboundMessage 显式指定了 context_id_override，直接使用
2. 否则根据 channel + chat_id 的映射表查找 user_id
3. 未知路由 → 抛出 UnknownRouteError
"""

from __future__ import annotations

from typing import Any

from nanobee.exceptions import UnknownRouteError as _UnknownRouteError

from nanobee.utils.logger import logger



class UnknownRouteError(_UnknownRouteError):
    """未知路由异常：channel 消息无法映射到任何用户。"""


class ContextRouter:
    """上下文路由器

    将 channel 消息路由到正确的用户上下文。
    路由表可从配置加载，也可通过事件动态更新。
    """

    def __init__(self, routing_map: dict[str, str] | None = None) -> None:
        """初始化路由器

        Args:
            routing_map: 路由映射表
                key 格式: "{channel}:{chat_id}"
                value: user_id
                示例: {"cli:default": "user-alice", "http:chat-123": "user-bob"}
        """
        self._routing: dict[str, str] = dict(routing_map or {})

    def resolve(
        self,
        channel: str,
        chat_id: str,
        *,
        override: str | None = None,
    ) -> tuple[str, str]:
        """将 channel 消息解析为 (user_id, session_id) 元组。

        优先级：
        1. override（由 InboundMessage.context_id_override 传入）
        2. 路由表匹配 (channel:chat_id)
        3. 路由表通配匹配 (channel:*)
        4. 抛出 UnknownRouteError

        Args:
            channel: 来源通道（如 "cli", "http"）
            chat_id: 通道内会话标识
            override: 显式覆盖的 user_id

        Returns:
            (用户标识, 会话 ID) 元组

        Raises:
            UnknownRouteError: 无法路由
        """
        # 1. 显式覆盖
        if override:
            return override, f"{channel}:{chat_id}"

        # 2. 精确匹配
        key = f"{channel}:{chat_id}"
        if key in self._routing:
            return self._routing[key], key

        # 3. 通配匹配 channel:*
        wildcard_key = f"{channel}:*"
        if wildcard_key in self._routing:
            return self._routing[wildcard_key], key

        # 4. 未知路由
        raise UnknownRouteError(channel, chat_id)

    def set_route(self, channel: str, chat_id: str, user_id: str) -> None:
        """设置或更新单条路由

        Args:
            channel: 来源通道
            chat_id: 通道内会话标识（可用 * 作为通配符）
            user_id: 目标用户标识
        """
        key = f"{channel}:{chat_id}"
        old = self._routing.get(key)
        self._routing[key] = user_id
        logger.info("路由更新: {} → {}（原: {}）", key, user_id, old)

    def remove_route(self, channel: str, chat_id: str) -> bool:
        """移除路由

        Args:
            channel: 来源通道
            chat_id: 通道内会话标识

        Returns:
            是否成功移除
        """
        key = f"{channel}:{chat_id}"
        if key in self._routing:
            del self._routing[key]
            logger.info("路由已移除: {}", key)
            return True
        return False

    def load_from_config(self, routing_config: dict[str, Any]) -> None:
        """从配置加载路由表

        Args:
            routing_config: 路由配置字典
                格式: {"cli:default": "user-alice", ...}
        """
        for key, value in routing_config.items():
            if isinstance(key, str) and isinstance(value, str):
                self._routing[key] = value
        logger.info("已从配置加载 {} 条路由", len(routing_config))

    @property
    def mapping(self) -> dict[str, str]:
        """获取当前路由映射的只读副本"""
        return dict(self._routing)

    def __repr__(self) -> str:
        return f"ContextRouter(routes={len(self._routing)})"


__all__ = [
    "ContextRouter",
    "UnknownRouteError",
]
