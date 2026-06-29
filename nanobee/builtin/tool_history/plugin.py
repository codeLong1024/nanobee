"""Tool History 插件 — 历史消息管理工具。

提供两个工具：
- trim_history(n)：粗暴截断，保留最近 N 条，删除更早消息（不可撤销）
- consolidate_history(summary, keep_last_n)：智能压缩，LLM 生成摘要后归档旧消息并裁剪
纯机制：框架不关心 LLM 何时调用、N 设多少，只提供裁剪刀和压缩器。
"""

from __future__ import annotations

from typing import Any

from nanobee.kernel.context_sandbox_var import current_request_context
from nanobee.plugins import ToolPlugin
from nanobee.utils.logger import logger


class ToolHistoryPlugin(ToolPlugin):
    """历史消息管理工具插件。

    暴露两个工具：
    - trim_history(n)：粗暴裁剪，保留最近 n 条
    - consolidate_history(summary, keep_last_n)：智能压缩，归档摘要 + 裁剪

    LLM 自主决定何时调用、使用哪个、参数值。
    框架只提供裁剪和压缩机制，不持有策略。

    线程安全：通过 CURRENT_REQUEST_CONTEXT ContextVar 按 turn 注入会话上下文，
    替代旧版 set_context() 实例属性写入模式。
    """

    # ------------------------------------------------------------------
    # 工具定义
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """返回 trim_history 和 consolidate_history 工具的 OpenAI function schema。"""
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
                        "对比 consolidate_history：trim_history 不保留摘要，直接丢弃旧消息。"
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
            {
                "type": "function",
                "function": {
                    "name": "consolidate_history",
                    "description": (
                        "智能压缩对话历史：将早期消息归档（追加摘要到 .consolidation.jsonl），"
                        "裁剪到 keep_last_n 条，并在开头注入 system 消息含历史摘要。"
                        "相比 trim_history 的粗暴截断，此工具保留历史摘要供后续参考。"
                        "应在以下情况调用：① token 占比 >70% 需要释放上下文时；"
                        "② 已完成一轮详细讨论，想压缩历史但保留关键信息时。"
                        "调用前：先阅读待压缩范围的对话历史，自行编写摘要文本。"
                        "摘要应覆盖：关键决策、用户偏好、重要事实、未完成任务。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": (
                                    "由你编写的早期对话摘要。覆盖关键决策、用户偏好、"
                                    "重要事实、未完成任务。不要包含系统内部消息的小结。"
                                ),
                            },
                            "keep_last_n": {
                                "type": "integer",
                                "description": "保留最近 N 条消息不压缩（默认 8）",
                                "minimum": 2,
                                "default": 8,
                            },
                        },
                        "required": ["summary"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行 trim_history 或 consolidate_history 工具。

        Args:
            tool_name: 工具名称（"trim_history" 或 "consolidate_history"）
            **kwargs: 工具参数

        Returns:
            执行结果字符串

        Raises:
            ValueError: 未知工具名或参数无效
        """
        if tool_name not in ("trim_history", "consolidate_history"):
            raise ValueError(f"未知工具: {tool_name}")

        # 从 per-turn ContextVar 获取路由上下文（线程安全）
        rctx = current_request_context()
        if rctx is None:
            return f"错误: 无法获取当前会话上下文，{tool_name} 未能执行"

        if not self.kernel or not self.kernel.session_manager:
            return f"错误: session_manager 不可用，{tool_name} 未能执行"

        self._user_id = rctx.context_id
        self._session_id = rctx.session_id

        try:
            if tool_name == "trim_history":
                return await self._execute_trim_history(kwargs)
            else:
                return await self._execute_consolidate_history(kwargs)
        except Exception:
            logger.exception(
                "{} 执行失败: user_id={}", tool_name, self._user_id,
            )
            return f"错误: {tool_name} 执行失败，请检查日志"

    # ------------------------------------------------------------------
    # 工具实现
    # ------------------------------------------------------------------

    async def _execute_trim_history(self, kwargs: dict[str, Any]) -> str:
        """执行 trim_history 工具。

        Args:
            kwargs: 工具参数字典，含 n（保留条数）。

        Returns:
            执行结果字符串。
        """
        n = kwargs.get("n")
        if not isinstance(n, int) or n < 2:
            return "错误: n 必须为 ≥2 的整数"

        session_manager = self.kernel.session_manager
        session = session_manager.get_or_create(self._user_id, self._session_id)
        before = len(session.messages)
        if before <= n:
            return f"历史共 {before} 条消息，未超过保留数 {n}，无需裁剪"

        session.trim_to_last_n(n)
        session_manager.save(session)
        after = len(session.messages)
        logger.info(
            "trim_history: 用户 {} (session={}) 历史裁剪 {} → {} 条",
            self._user_id, self._session_id, before, after,
        )
        return f"历史裁剪完成：{before} → {after} 条消息"

    async def _execute_consolidate_history(self, kwargs: dict[str, Any]) -> str:
        """执行 consolidate_history 工具。

        Args:
            kwargs: 工具参数字典，含 summary（摘要文本）、keep_last_n（保留条数，默认 8）。

        Returns:
            执行结果字符串。
        """
        summary = kwargs.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            return (
                "错误: summary 不能为空。请阅读历史对话后编写一份摘要。"
                "摘要应覆盖：关键决策、用户偏好、重要事实、未完成任务。"
            )

        keep_last_n = kwargs.get("keep_last_n", 8)
        if not isinstance(keep_last_n, int) or keep_last_n < 2:
            return "错误: keep_last_n 必须为 ≥2 的整数"

        session_manager = self.kernel.session_manager
        try:
            result = session_manager.consolidate(
                user_id=self._user_id,
                session_id=self._session_id,
                summary=summary.strip(),
                keep_last_n=keep_last_n,
            )
        except ValueError as e:
            return f"错误: {e}"

        if result["archived_count"] == 0:
            return (
                f"无需压缩：当前仅 {result['before_count']} 条消息，"
                f"未超过保留数 {keep_last_n}"
            )

        return (
            f"历史压缩完成：{result['before_count']} → {result['after_count']} 条消息。"
            f"已归档 {result['archived_count']} 条消息到 # {result['archived_index']}，"
            f"历史摘要已注入为 system 消息。"
        )
