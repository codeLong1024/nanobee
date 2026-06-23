"""Subagent 工具 —— 让 LLM 委托后台任务。

遵循框架无知论：工具只提供 spawn/list/cancel 机制，不持策略。
并发限制等策略性决策通过配置暴露给部署方。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobee.agent.tools.base import Tool
from nanobee.kernel.context_sandbox_var import (
    current_request_context, current_sandbox,
)

if TYPE_CHECKING:
    from nanobee.agent.subagent import SubagentManager


class SpawnSubagentTool(Tool):
    """在后台启动一个子代理执行独立任务。"""

    name = "spawn_subagent"
    description = (
        "在后台启动一个子代理执行独立任务。子代理拥有独立的 LLM 调用和工具执行能力，"
        "完成后结果会自动注入回当前对话。适用于执行长期、耗时、或可并行的后台任务。"
    )

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "子代理的执行目标，需要清晰完整，包含所有必要上下文。",
                },
                "label": {
                    "type": "string",
                    "description": "可读标签，用于在状态追踪时标识任务。",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str = "") -> str:
        rctx = current_request_context()
        ctx_id = rctx.context_id if rctx else "default"
        channel = rctx.channel if rctx else "cli"
        chat_id = rctx.chat_id if rctx else "direct"
        session_id = rctx.session_id if rctx else None
        sandbox = current_sandbox()
        return await self._manager.spawn(
            task=task,
            label=label,
            context_id=ctx_id,
            origin_channel=channel,
            origin_chat_id=chat_id,
            origin_session_id=session_id,
            sandbox=sandbox,
        )


class ListSubagentsTool(Tool):
    """列出当前对话中所有运行中的子代理。"""

    name = "list_subagents"

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @property
    def description(self) -> str:
        return "列出当前对话中所有运行中的子代理。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        rctx = current_request_context()
        ctx_id = rctx.context_id if rctx else "default"
        count = self._manager.get_running_count_by_session(ctx_id)
        return (
            f"当前有 {count} 个子代理正在运行。\n"
            "子代理完成后会自动通知，无需手动检查。"
        )


__all__ = [
    "SpawnSubagentTool",
    "ListSubagentsTool",
]
