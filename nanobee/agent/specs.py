"""Agent 执行相关的数据类、类型定义与工具函数。

将 AgentRunSpec、AgentRunResult、PluginHooks 等共享类型从 runner.py 提取到此模块，
避免 tool_pipeline.py → runner.py → tool_pipeline.py 的循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

from nanobee.agent.hook import AgentHook
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."


class PluginHooks(TypedDict, total=False):
    """插件 Hook 回调字典。

    Attributes:
        pre_invoke: 工具执行前拦截钩子，签名 (tool_name: str, args: dict) → args
        post_invoke: 工具执行后拦截钩子，签名 (tool_name: str, result: Any) → result
    """

    pre_invoke: list[Callable[[str, dict[str, Any]], dict[str, Any]]]
    post_invoke: list[Callable[[str, Any], Any]]


def _inject_context_to_tool(tool: Any, spec: AgentRunSpec) -> None:
    """调用工具插件的 set_context() 注入会话上下文。

    用于工具插件（如 tool_cron）在每次工具执行前获取当前会话信息。
    只对有 set_context 方法的工具插件调用。

    Args:
        tool: 工具实例（可能是 ToolPluginAdapter 或直接工具）
        spec: Agent 执行配置，包含通道上下文信息
    """
    # 从 ToolPluginAdapter 中提取底层插件
    plugin = None
    if hasattr(tool, "_plugin"):
        plugin = tool._plugin

    if plugin is None:
        # 尝试直接从工具实例获取（如果不是适配器）
        if hasattr(tool, "set_context"):
            plugin = tool

    if plugin is None:
        return

    # 调用 set_context（如果存在）
    if hasattr(plugin, "set_context") and callable(getattr(plugin, "set_context")):
        try:
            plugin.set_context(
                channel=spec.channel,
                chat_id=spec.chat_id,
                user_id=spec.sender_id,
                metadata=spec.metadata,
                session_key=spec.session_id,
            )
        except Exception:
            logger.debug("调用工具插件 set_context 失败: {}", type(plugin).__name__)


@dataclass(slots=True)
class AgentRunSpec:
    """Agent 单次执行的配置。"""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    context_id: str | None = None
    trace_id: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    filtered_tool_names: list[str] | None = None
    plugin_hooks: PluginHooks | None = None
    # 通道上下文（用于工具插件的 set_context 调用）
    channel: str = ""
    chat_id: str = ""
    sender_id: str = ""
    session_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    # 需要节流的外部查询工具名集合，从插件 metadata.throttle_group 构建
    throttled_tool_names: dict[str, str] = field(default_factory=dict)
    # 具有命令执行能力的工具名集合，从插件 metadata.exec_capable 构建
    exec_capable_tools: set[str] = field(default_factory=set)
    # 具有文件编辑能力的工具名集合，从插件 metadata.file_edit_capability 构建
    file_edit_tools: set[str] = field(default_factory=set)


@dataclass(slots=True)
class AgentRunResult:
    """Agent 单次执行的最终结果。"""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
