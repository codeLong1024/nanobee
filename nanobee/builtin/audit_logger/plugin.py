"""
audit_logger 参考插件 —— 监听 on_message_completed 输出审计日志

Phase 3 参考插件：纯监听型插件，不贡献提示词或工具，
仅在每轮 Agent 交互完成后通过 logging 记录交互摘要。
"""

from __future__ import annotations

from typing import Any

from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.logger import logger



class AuditLoggerPlugin(NanobeePlugin):
    """极简审计日志插件：在每轮对话完成时记录交互摘要。

    不实现 ``contribute_to_prompt`` / ``contribute_to_tools``，
    仅通过 ``on_message_completed`` 监听对话完成事件。
    内部维护 ``call_count`` 用于测试验证。
    """

    name = "audit_logger"
    version = "1.0.0"
    plugin_type = "audit"

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        self._call_count: dict[str, int] = {}

    async def on_message_completed(
        self,
        context: Any,
        messages: list[dict[str, Any]],
    ) -> None:
        """记录本轮交互摘要。

        统计本轮总消息数、LLM 迭代次数和工具调用次数。
        ``round`` 计数器按 ``context.user_id`` 区分。

        Args:
            context: UserContext 实例
            messages: 本轮完整的消息列表
        """
        user_id = getattr(context, "user_id", "?")
        self._call_count[user_id] = self._call_count.get(user_id, 0) + 1
        tool_calls = sum(1 for m in messages if isinstance(m, dict) and "tool_calls" in m)
        iterations = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")

        logger.info(
            "[audit] user={} | round={} | messages={} | iterations={} | tool_calls={}",
            user_id,
            self._call_count[user_id],
            len(messages),
            iterations,
            tool_calls,
        )

    @property
    def call_count(self) -> int:
        """获取所有用户的累计被调用次数，用于测试验证。"""
        return sum(self._call_count.values())
