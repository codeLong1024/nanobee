"""Subagent manager for background task execution.

移植自 nanobot/agent/subagent.py，适配 nanobee 沙箱体系。
沙箱通过 ContextVar (context_sandbox_var) 注入，无需参数透传。
工具通过 ToolRegistry + ToolCollector 管理。
结果注入通过回调模式实现。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nanobee.agent.hook import AgentHook, AgentHookContext
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.config.schema import AgentDefaults, ToolsConfig
from nanobee.kernel.context_sandbox_var import bind_sandbox, reset_sandbox
from nanobee.kernel.sandbox import ContextSandbox
from nanobee.kernel.tool_collector import ToolCollector
from nanobee.providers.base import LLMProvider
from nanobee.utils.helpers import build_runtime_context
from nanobee.utils.logger import logger
from nanobee.utils.prompt_templates import render_template


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


# 子代理自身工具（禁止子代理递归 spawn）
_SELF_REF_TOOLS: frozenset[str] = frozenset({"spawn_subagent", "list_subagents"})


class SubagentManager:
    """Manages background subagent execution.

    适配 nanobee 沙箱：使用 ContextSandbox + ContextVar 模式。
    工具通过 ToolCollector 按权限过滤。
    结果通过 inject_result 回调注入主 Agent 对话。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        *,
        max_tool_result_chars: int = 65536,
        model: str | None = None,
        tools_registry: ToolRegistry | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        skills_summary_builder: Callable[[Path | None], str] | None = None,
        result_injector: Callable[[str, str, dict[str, Any]], Any] | None = None,
        event_bus: Any = None,
    ):
        """初始化 SubagentManager。

        Args:
            provider: LLM provider 实例。
            workspace: 默认工作区路径。
            max_tool_result_chars: 工具结果最大字符数。
            model: 模型名称，默认使用 provider 默认模型。
            tools_registry: 工具注册表（全局）。None 时使用空注册表。
            restrict_to_workspace: 是否限制工作区访问。
            disabled_skills: 禁用的技能名称列表。
            max_iterations: 最大迭代次数，默认从 AgentDefaults 读取。
            max_concurrent_subagents: 最大并发子代理数。
            llm_wall_timeout_for_session: 按 session key 获取 LLM 超时。
            skills_summary_builder: 技能摘要构建器，签名 (workspace: Path) -> str。
            result_injector: 结果注入回调。
                签名 async (content: str, context_id: str, metadata: dict) -> None。
                用于将 subagent 结果注入回主 Agent 对话。
            event_bus: 事件总线实例。用于发布 subagent 生命周期事件。
        """
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else 4  # nanobee 默认最大并发子代理
        )
        self.runner = AgentRunner(provider)
        self._tools_registry = tools_registry if tools_registry is not None else ToolRegistry()
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._skills_summary_builder = skills_summary_builder
        self._result_injector = result_injector
        self._event_bus = event_bus
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # context_id -> {task_id, ...}

    def _build_sandbox(self, workspace: Path) -> ContextSandbox:
        """构建沙箱实例。

        Args:
            workspace: 工作区根目录。

        Returns:
            ContextSandbox 实例。
        """
        return ContextSandbox(context_root=workspace)

    def _build_tools(
        self,
        workspace: Path,
        *,
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
    ) -> ToolRegistry:
        """构建子代理的工具注册表。

        从全局工具注册表过滤出子代理可用的工具。
        自动排除子代理自身工具（spawn_subagent/list_subagents），防止递归 spawn。

        Args:
            workspace: 工作区路径。
            whitelist: 工具白名单（None 或空 = 全部允许）。
            blacklist: 工具黑名单（None 或空 = 无禁用）。

        Returns:
            ToolRegistry 实例，仅包含允许的工具。
        """
        if not self._tools_registry:
            return ToolRegistry()

        # 自动加入子代理自身工具到黑名单，禁止递归 spawn
        effective_blacklist: list[str] = list(_SELF_REF_TOOLS)
        if blacklist:
            effective_blacklist.extend(blacklist)

        all_names = self._tools_registry.tool_names
        collector = ToolCollector(
            tool_names=all_names,
            whitelist=whitelist,
            blacklist=effective_blacklist,
        )
        allowed = set(collector.allowed_tools)

        registry = ToolRegistry()
        for name in all_names:
            if name in allowed:
                tool = self._tools_registry.get(name)
                if tool is not None:
                    registry.register(tool)
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """更新 LLM provider 和模型。"""
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    def set_result_injector(
        self,
        injector: Callable[[str, str, dict[str, Any]], Any],
    ) -> None:
        """设置结果注入回调。

        Args:
            injector: async (content: str, context_id: str, metadata: dict) -> None。
        """
        self._result_injector = injector

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        origin_session_id: str | None = None,
        context_id: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        sandbox: ContextSandbox | None = None,
        tool_whitelist: list[str] | None = None,
        tool_blacklist: list[str] | None = None,
    ) -> str:
        """在后台启动一个子代理执行任务。

        Args:
            task: 任务描述。
            label: 可读标签（用于状态显示）。None 时自动截取 task 前 30 字符。
            origin_channel: 来源通道名。
            origin_chat_id: 来源会话 ID。
            origin_session_id: 来源 session ID（用于结果路由回原 session）。
            context_id: 上下文 ID，用于结果注入路由。
            origin_message_id: 原始消息 ID。
            temperature: LLM 温度参数。
            sandbox: 沙箱实例。None 时自动从 workspace 构建。
            tool_whitelist: 工具白名单。
            tool_blacklist: 工具黑名单。

        Returns:
            子代理启动确认消息字符串。
        """
        # 并发限制检查
        running_count = len(self._running_tasks)
        if running_count >= self.max_concurrent_subagents:
            return (
                f"Maximum concurrent subagents ({self.max_concurrent_subagents}) "
                f"reached. Please wait for existing ones to complete before retrying."
            )

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_id": origin_session_id,
            "context_id": context_id,
        }

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                sandbox,
                tool_whitelist,
                tool_blacklist,
            )
        )
        self._running_tasks[task_id] = bg_task
        if context_id:
            self._session_tasks.setdefault(context_id, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if context_id and (ids := self._session_tasks.get(context_id)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[context_id]

        bg_task.add_done_callback(_cleanup)

        # 发布 EventBus 事件 — 通知 loop 立即发送用户可见通知
        if self._event_bus is not None:
            await self._event_bus.publish("subagent.spawned", {
                "task_id": task_id,
                "label": display_label,
                "task": task[:200],
                "context_id": context_id,
                "channel": origin_channel,
                "chat_id": origin_chat_id,
            })

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return (
            f"Subagent [{display_label}] spawned (id: {task_id}). "
            f"(A notification has already been sent to the user. "
            f"No need to repeat or confirm — continue with your next step.)"
        )

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        sandbox: ContextSandbox | None = None,
        tool_whitelist: list[str] | None = None,
        tool_blacklist: list[str] | None = None,
    ) -> None:
        """执行子代理任务并宣布结果。

        使用 nanobee 的 ContextVar 沙箱注入机制：
        - 在子代理执行前 bind_sandbox
        - 执行完成后 reset_sandbox
        工具执行时通过 current_sandbox() 自动获取沙箱实例。
        """
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            root = self.workspace
            actual_sandbox = sandbox or self._build_sandbox(root)

            tools = self._build_tools(
                workspace=root,
                whitelist=tool_whitelist,
                blacklist=tool_blacklist,
            )
            system_prompt = self._build_subagent_prompt(workspace=root)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            ctx_id = origin.get("context_id")
            llm_timeout = (
                self._llm_wall_timeout_for_session(ctx_id)
                if self._llm_wall_timeout_for_session
                else None
            )

            # 通过 ContextVar 绑定沙箱，工具执行时自动获取
            sandbox_token = bind_sandbox(actual_sandbox)
            try:
                result = await self.runner.run(AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    temperature=temperature,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message=(
                        "Task completed but no final response was generated."
                    ),
                    error_message=None,
                    fail_on_tool_error=True,
                    checkpoint_callback=_on_checkpoint,
                    context_id=ctx_id,
                    workspace=root,
                    llm_timeout_s=llm_timeout,
                ))
            finally:
                reset_sandbox(sandbox_token)

            status.phase = "done"
            status.stop_reason = result.stop_reason

            if result.stop_reason == "tool_error":
                status.tool_events = list(result.tool_events)
                await self._announce_result(
                    task_id, label, task,
                    self._format_partial_progress(result),
                    origin, "error", origin_message_id,
                )
            elif result.stop_reason == "error":
                await self._announce_result(
                    task_id, label, task,
                    result.error or "Error: subagent execution failed.",
                    origin, "error", origin_message_id,
                )
            else:
                final_result = (
                    result.final_content
                    or "Task completed but no final response was generated."
                )
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(
                    task_id, label, task, final_result,
                    origin, "ok", origin_message_id,
                )

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(
                task_id, label, task, f"Error: {e}",
                origin, "error", origin_message_id,
            )

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """通过结果注入回调宣布子代理结果。

        结果注入回调由外部设置（如 AgentLoop），
        负责将结果路由到主 Agent 的待处理队列。
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        ctx_id = origin.get("context_id") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
            "subagent_status": status,
            "origin_channel": origin.get("channel", "system"),
            "origin_chat_id": origin.get("chat_id", ""),
            "origin_session_id": origin.get("session_id"),
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id

        # 发布 EventBus 事件（供通道侧感知和测试断言）
        if self._event_bus is not None:
            await self._event_bus.publish(f"subagent.{status}", {
                "context_id": ctx_id,
                "task_id": task_id,
                "label": label,
                "content": announce_content[:500],
                "metadata": metadata,
            })

        if self._result_injector is not None:
            try:
                await self._result_injector(announce_content, ctx_id, metadata)
            except Exception:
                logger.exception(
                    "Subagent [{}] result injector failed", task_id,
                )
        else:
            logger.warning(
                "Subagent [{}] result injector not set, result dropped: {}",
                task_id, announce_content[:200],
            )

        logger.debug(
            "Subagent [{}] announced result to {}:{}",
            task_id, origin.get('channel', '?'), origin.get('chat_id', '?'),
        )

    @staticmethod
    def _format_partial_progress(result) -> str:
        """格式化部分进度（工具执行失败时）。"""
        completed = [
            e for e in result.tool_events if e["status"] == "ok"
        ]
        failure = next(
            (e for e in reversed(result.tool_events) if e["status"] == "error"),
            None,
        )
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (
            result.error or "Error: subagent execution failed."
        )

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """构建子代理的系统提示词。

        使用 nanobee 的 prompt_templates 和 helpers.build_runtime_context。
        技能摘要通过 _skills_summary_builder 回调获取。
        """
        time_ctx = build_runtime_context()
        root = workspace or self.workspace

        skills_summary = ""
        if self._skills_summary_builder is not None:
            try:
                skills_summary = self._skills_summary_builder(root)
            except Exception:
                logger.exception("Failed to build skills summary for subagent")

        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, context_id: str) -> int:
        """取消指定上下文的所有子代理。返回已取消的数量。"""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(context_id, [])
            if tid in self._running_tasks
            and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """返回当前正在运行的子代理数量。"""
        return len(self._running_tasks)

    def get_running_count_by_session(self, context_id: str) -> int:
        """返回指定上下文中正在运行的子代理数量。"""
        tids = self._session_tasks.get(context_id, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks
            and not self._running_tasks[tid].done()
        )
