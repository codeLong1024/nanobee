"""Tool History 插件 — 历史消息裁剪工具。

提供 trim_history 工具：保留最近 N 条历史消息，删除更早的。
纯机制：框架不关心 LLM 何时调用、N 设多少，只提供裁剪刀。
"""

from __future__ import annotations

from typing import Any

from nanobee.plugins.tool import ToolPlugin
from nanobee.utils.logger import logger


class ToolHistoryPlugin(ToolPlugin):
    """历史消息裁剪工具插件。

    暴露 trim_history(n) 工具：
    - 内部调用 UserContext.trim_to_last_n(n)
    - LLM 自主决定 n 值（基于对话上下文、token 占比等信息密度判断）
    - 框架只提供裁剪机制，不持有策略
    """

    name = "tool_history"
    version = "1.0.0"
    plugin_type = "tool"

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)
        self._user_id: str = ""

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------

    def set_context(
        self,
        channel: str = "",
        chat_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """注入当前会话上下文（用于定位用户历史记录）。

        Args:
            channel: 通道名称
            chat_id: 会话 ID
            user_id: 用户 ID（用作 context_id）
            metadata: 通道附加元数据
        """
        self._user_id = user_id

    # ------------------------------------------------------------------
    # 工具定义
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """返回 trim_history 工具的 OpenAI function schema。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "trim_history",
                    "description": (
                        "裁剪对话历史，保留最近 n 条消息，删除更早的消息。"
                        "应在以下情况调用：① 已将关键信息存入 memory/ 后；"
                        "② 历史过长影响上下文质量时。n 值由你自主判断（建议 10-30 条）。"
                        "注意：此操作不可撤销，被删除的消息无法恢复。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "n": {
                                "type": "integer",
                                "description": "保留的最近消息条数",
                                "minimum": 2,
                            },
                        },
                        "required": ["n"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行 trim_history 工具。

        Args:
            tool_name: 工具名称（固定为 "trim_history"）
            **kwargs: 工具参数，包含 n（保留条数）

        Returns:
            执行结果字符串

        Raises:
            ValueError: 未知工具名或参数无效
        """
        if tool_name != "trim_history":
            raise ValueError(f"未知工具: {tool_name}")

        n = kwargs.get("n")
        if not isinstance(n, int) or n < 2:
            return "错误: n 必须为 ≥2 的整数"

        if not self._user_id:
            return "错误: 无法获取当前用户上下文，trim_history 未能执行"

        if not self.kernel or not self.kernel.context_manager:
            return "错误: context_manager 不可用，trim_history 未能执行"

        try:
            user_ctx = await self.kernel.context_manager.get_or_create(self._user_id)
            before = len(user_ctx.get_messages())
            if before <= n:
                return f"历史共 {before} 条消息，未超过保留数 {n}，无需裁剪"

            user_ctx.trim_to_last_n(n)
            after = len(user_ctx.get_messages())
            logger.info(
                "trim_history: 用户 {} 历史裁剪 {} → {} 条",
                self._user_id, before, after,
            )
            return f"历史裁剪完成：{before} → {after} 条消息"

        except Exception:
            logger.exception("trim_history 执行失败: user_id={}, n={}", self._user_id, n)
            return "错误: trim_history 执行失败，请检查日志"
