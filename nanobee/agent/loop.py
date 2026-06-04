"""Agent Loop - 核心消息调度引擎。

核心保留：TurnState 状态机（RESTORE→COMPACT→COMMAND→BUILD→RUN→SAVE→RESPOND→DONE）、
_process_message 驱动循环、上下文治理、工具执行编排。
改造点：Session→ContextManager、ContextBuilder→ContextPipeline、
命令路由移除、进度Hook→EventBus、工具注册走 PluginManager。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import logging

logger = logging.getLogger(__name__)

from nanobee.agent import model_presets as preset_helpers
from nanobee.agent.hook import AgentHook, CompositeHook
from nanobee.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobee.agent.tools.registry import ToolRegistry, ToolPluginAdapter
from nanobee.providers.base import LLMProvider
from nanobee.providers.factory import ProviderSnapshot
from nanobee.utils.observability import generate_trace_id, set_trace_id
from nanobee.utils.document import extract_documents
from nanobee.utils.helpers import (
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    truncate_text,
)
from nanobee.utils.image_generation_intent import image_generation_prompt as image_gen_prompt_fn
from nanobee.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobee.config.schema import AgentDefaults, ModelPresetConfig
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.kernel.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.plugin_manager import PluginManager
from nanobee.kernel.router import ContextRouter, UnknownRouteError


# 入站消息数据类
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

    @property
    def context_id(self) -> str:
        """获取上下文 ID。"""
        return self.context_id_override or f"{self.channel}:{self.chat_id}"


# 出站消息数据类
@dataclass
class OutboundMessage:
    """发送到聊天通道的消息。"""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TurnState(Enum):
    """状态机状态枚举。"""
    RESTORE = auto()
    COMPACT = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    """状态流转追踪条目。"""
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    """单次 Turn 的运行时上下文。"""
    msg: InboundMessage
    context_id: str
    state: TurnState
    turn_id: str

    # 对话历史（从 ContextManager 获取）
    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace_id: str = field(default_factory=generate_trace_id)
    trace: list[StateTraceEntry] = field(default_factory=list)


class AgentLoop:
    """Agent 核心处理引擎。

    职责：
    1. 接收消息
    2. 构建上下文（历史 + 系统提示词）
    3. 调用 LLM
    4. 执行工具调用
    5. 保存结果并发送响应
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    # 事件驱动的状态转换表
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.BUILD,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        *,
        context_manager: ContextManager,
        context_pipeline: ContextPipeline,
        event_bus: EventBus | None = None,
        plugin_manager: PluginManager | None = None,
        router: ContextRouter | None = None,
        model: str | None = None,
        max_iterations: int = 10,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int = 65536,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        mcp_servers: dict | None = None,
        hooks: list[AgentHook] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
    ) -> None:
        self.provider = provider
        self.workspace = workspace
        self.context_manager = context_manager
        self.context_pipeline = context_pipeline
        self.event_bus = event_bus
        self.plugin_manager = plugin_manager
        self._router = router or ContextRouter()
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader

        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = max_tool_result_chars
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = tool_hint_max_length

        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)

        self.tools = ToolRegistry()
        self.runner = AgentRunner(provider)
        self._extra_hooks: list[AgentHook] = hooks or []

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False

        # 上下文级互斥锁：按用户粒度隔离并发
        # 同一 user_id 串行，不同 user_id 并行
        _max = int(os.environ.get("NANOBEE_MAX_CONCURRENT_REQUESTS", "3"))
        self._lock_manager = LockManager(max_concurrent=_max)
        # 每个上下文的活跃任务列表
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        # 每个上下文的待处理消息队列（中轮注入）
        self._pending_queues: dict[str, asyncio.Queue] = {}

        self._register_plugin_tools()
        self._current_iteration: int = 0

    @classmethod
    def from_kernel(
        cls,
        kernel: Any,
        provider: LLMProvider,
        workspace: Path,
        **extra: Any,
    ) -> AgentLoop:
        """从 NanobeeKernel 创建 AgentLoop。"""
        return cls(
            provider=provider,
            workspace=workspace,
            context_manager=kernel.context_manager,
            context_pipeline=kernel.context_pipeline,
            event_bus=kernel.event_bus,
            plugin_manager=kernel.plugin_manager,
            router=getattr(kernel, "router", None),
            **extra,
        )

    def _register_plugin_tools(self) -> None:
        """从 PluginManager 注册工具插件到 ToolRegistry。

        注意：此方法需要在插件加载完成后调用（即在 Kernel.boot() 之后）。
        如果在插件加载前调用，会注册 0 个工具。
        """
        if self.plugin_manager is None:
            return
        tool_plugins = self.plugin_manager.get_by_type("tool")
        if not tool_plugins:
            # 插件尚未加载，跳过注册（在 boot() 中会重新注册）
            return
        registered: list[str] = []
        for plugin in tool_plugins:
            try:
                tool_defs = plugin.get_tools()
                for tool_def in tool_defs:
                    adapter = ToolPluginAdapter(plugin, tool_def)
                    self.tools.register(adapter)
                    registered.append(adapter.name)
            except Exception:
                logger.exception("注册工具插件 %s 失败", getattr(plugin, "name", "unknown"))
        logger.info("注册了 %s 个工具插件: %s", len(registered), registered)

    # ---- Phase 2: 插件 Hook 集成 ----

    def _get_enabled_plugins(self) -> list[Any]:
        """获取所有已启用的插件。"""
        if self.plugin_manager is None:
            return []
        return [
            self.plugin_manager._plugins[name]
            for name in self.plugin_manager.list_plugins()
            if self.plugin_manager._plugins[name].is_enabled
        ]

    def _collect_plugin_prompts(self, user_ctx: Any) -> str:
        """收集所有已启用插件贡献的提示词内容。

        Args:
            user_ctx: 当前用户上下文（UserContext 实例）

        Returns:
            拼装后的插件贡献文本，无贡献时返回空字符串
        """
        contributions: list[str] = []
        for plugin in self._get_enabled_plugins():
            try:
                content = plugin.contribute_to_prompt(user_ctx)
                if content:
                    contributions.append(content)
            except Exception:
                logger.exception("插件 %s.contribute_to_prompt 出错", getattr(plugin, "name", "?"))
        return "\n\n".join(contributions) if contributions else ""

    def _collect_plugin_tools(
        self,
        user_ctx: Any,
        current_tool_names: list[str],
    ) -> list[str]:
        """让所有已启用插件修改工具列表。

        Args:
            user_ctx: 当前用户上下文（UserContext 实例）
            current_tool_names: 当前已注册的工具名称列表

        Returns:
            插件修改后的工具名称列表
        """
        tool_names = list(current_tool_names)
        for plugin in self._get_enabled_plugins():
            try:
                tool_names = plugin.contribute_to_tools(user_ctx, tool_names)
            except Exception:
                logger.exception("插件 %s.contribute_to_tools 出错", getattr(plugin, "name", "?"))
        return tool_names

    async def _notify_plugins_message_completed(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """通知所有已启用插件对话轮次已完成。

        Args:
            context_id: 用户上下文 ID
            messages: 本轮完整的消息列表
        """
        try:
            user_ctx = await self.context_manager.get_or_create(context_id)
        except Exception:
            logger.debug("获取用户上下文失败，跳过 on_message_completed 通知")
            return

        for plugin in self._get_enabled_plugins():
            try:
                await plugin.on_message_completed(user_ctx, messages)
            except Exception:
                logger.exception("插件 %s.on_message_completed 出错", getattr(plugin, "name", "?"))

    async def _connect_mcp(self) -> None:
        """连接配置的 MCP 服务器（一次性，懒加载）。"""
        logger.info("MCP: 检查是否需要连接服务器 (connected=%s, connecting=%s, servers=%s)", 
                    self._mcp_connected, self._mcp_connecting, bool(self._mcp_servers))
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobee.agent.tools.mcp import connect_mcp_servers

        try:
            logger.info("MCP: 开始连接 %s 个服务器", len(self._mcp_servers))
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
                logger.info("MCP: 成功连接 %s 个服务器", len(self._mcp_stacks))
            else:
                logger.warning("MCP: 没有 MCP 服务器成功连接（下次消息时重试）")
        except asyncio.CancelledError:
            logger.warning("MCP 连接被取消（下次消息时重试）")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.warning("MCP 服务器连接失败（下次消息时重试）: %s", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _effective_context_id(self, msg: InboundMessage) -> str:
        """返回用于任务路由和中轮注入的上下文 ID（即 user_id）。

        路由优先级：
        1. msg.context_id_override 显式指定
        2. 路由器根据 channel:chat_id 查找
        3. 未知路由直接抛出异常
        """
        try:
            return self._router.resolve(
                msg.channel, msg.chat_id,
                override=msg.context_id_override,
            )
        except UnknownRouteError:
            # 保持向后兼容：如果没有路由器配置，降级使用 msg.context_id
            return msg.context_id

    def _replay_token_budget(self) -> int:
        """从上下文窗口大小推算历史重放的 token 预算。"""
        if not self.context_window_tokens or self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _build_initial_messages(
        self,
        msg: InboundMessage,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构建 LLM 的初始消息列表。"""
        # 使用 ContextPipeline 构建系统提示词（含插件 Hook 贡献）
        pipeline_context: dict[str, Any] = {
            "context_id": msg.context_id,
            "messages": history,
            "system_prompt": "",
        }

        # 获取用户上下文和已启用插件，用于 build_with_plugins()
        user_ctx = await self.context_manager.get_or_create(msg.context_id)
        plugins = self._get_enabled_plugins()
        system_prompt = await self.context_pipeline.build_with_plugins(
            pipeline_context, user_ctx, plugins,
        )

        # 构建消息列表：system + history + current_message
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 附加历史消息（受 token 预算限制）
        for entry in history:
            messages.append(entry)

        # 当前用户消息
        current_content = image_gen_prompt_fn(msg.content, msg.metadata)
        if msg.media:
            new_content, _ = extract_documents(current_content, msg.media)
            current_content = new_content

        if current_content:
            messages.append({"role": "user", "content": current_content})

        return messages

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        *,
        context_id: str,
        trace_id: str | None = None,
        sandbox: Any | None = None,
        filtered_tool_names: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """运行 Agent 迭代循环（LLM 调用 + 工具执行）。

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """排空待处理队列中的后续消息。"""
            if pending_queue is None:
                return []
            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    pending_msg = pending_queue.get_nowait()
                    text = getattr(pending_msg, "content", str(pending_msg))
                    if text.strip():
                        items.append({"role": "user", "content": text})
                except asyncio.QueueEmpty:
                    break
            return items

        hook: AgentHook = AgentHook()
        if self._extra_hooks:
            hook = CompositeHook(list(self._extra_hooks))

        # 构造插件 Hook 闭包列表（on_pre_invoke / on_post_invoke）
        plugin_hooks: dict[str, list[Any]] | None = None
        enabled_plugins = self._get_enabled_plugins()
        if enabled_plugins:
            try:
                user_ctx_for_hooks = await self.context_manager.get_or_create(context_id)
                pre_invoke_fns: list[Any] = []
                post_invoke_fns: list[Any] = []
                for p in enabled_plugins:
                    pre_invoke_fns.append(
                        lambda name, args, _p=p, _ctx=user_ctx_for_hooks: _p.on_pre_invoke(_ctx, name, args)
                    )
                    post_invoke_fns.append(
                        lambda name, result, _p=p, _ctx=user_ctx_for_hooks: _p.on_post_invoke(_ctx, name, result)
                    )
                if pre_invoke_fns or post_invoke_fns:
                    plugin_hooks = {
                        "pre_invoke": pre_invoke_fns,
                        "post_invoke": post_invoke_fns,
                    }
            except Exception:
                logger.debug("构造 plugin_hooks 失败，跳过工具 Hook")

        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            hook=hook,
            error_message="Sorry, I encountered an error calling the AI model.",
            concurrent_tools=True,
            workspace=self.workspace,
            context_id=context_id,
            trace_id=trace_id or generate_trace_id(),
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            progress_callback=on_progress,
            stream_progress_deltas=on_stream is not None,
            retry_wait_callback=on_retry_wait,
            injection_callback=_drain_pending,
            sandbox=sandbox,
            filtered_tool_names=filtered_tool_names,
            plugin_hooks=plugin_hooks,
        ))

        if result.stop_reason == "max_iterations":
            logger.warning("达到最大迭代次数 (%s)", self.max_iterations)
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM 返回错误: %s", (result.final_content or "")[:200])

        # 发射事件
        if self.event_bus:
            await self.event_bus.publish("agent.turn_completed", {
                "context_id": context_id,
                "final_content": result.final_content,
                "stop_reason": result.stop_reason,
                "tools_used": result.tools_used,
                "usage": result.usage,
            })

        # 通知插件对话轮次已完成（后台执行，不阻塞主流程）
        task = asyncio.create_task(
            self._notify_plugins_message_completed(context_id, result.messages)
        )
        task.add_done_callback(lambda t: t.exception() if t.exception() else None)

        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """启动 Agent Loop（持续运行，处理入站消息）。"""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop 已启动")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._consume_inbound(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                if not self._running:
                    raise
                continue
            except Exception as e:
                logger.warning("消费入站消息出错: %s, 继续处理...", e)
                continue

            effective_key = self._effective_context_id(msg)
            # 如果该上下文已有活跃的待处理队列，路由到那里
            if effective_key in self._pending_queues:
                try:
                    self._pending_queues[effective_key].put_nowait(msg)
                except asyncio.QueueFull:
                    logger.warning("上下文 %s 待处理队列已满，回退为排队任务", effective_key)
                else:
                    logger.info("后续消息已路由到上下文 %s 的待处理队列", effective_key)
                    continue

            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _consume_inbound(self) -> InboundMessage:
        """消费入站消息（由通道插件调用）。"""
        raise NotImplementedError("子类或集成层需要实现此方法")

    async def _dispatch(self, msg: InboundMessage) -> None:
        """处理消息：同用户串行，跨用户并行。"""
        context_id = self._effective_context_id(msg)

        # 注册待处理队列
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[context_id] = pending

        try:
            async with self._lock_manager.acquire(context_id):
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        stream_base_id = f"{msg.context_id}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream_fn(delta: str) -> None:
                            if self.event_bus:
                                await self.event_bus.publish("agent.stream_delta", {
                                    "context_id": context_id,
                                    "stream_id": _current_stream_id(),
                                    "delta": delta,
                                })

                        async def on_stream_end_fn(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            if self.event_bus:
                                await self.event_bus.publish("agent.stream_end", {
                                    "context_id": context_id,
                                    "stream_id": _current_stream_id(),
                                    "resuming": resuming,
                                })
                            stream_segment += 1

                        on_stream = on_stream_fn
                        on_stream_end = on_stream_end_fn

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self._publish_outbound(response)
                    else:
                        logger.debug("消息处理返回 None，不发送响应")
                except asyncio.CancelledError:
                    logger.info("上下文 %s 的任务被取消", context_id)
                    raise
                except Exception:
                    logger.exception("处理上下文 %s 的消息出错", context_id)
                    await self._publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
        finally:
            # 排空待处理队列，重新发布为独立入站消息
            queue = self._pending_queues.pop(context_id, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    leftover += 1
                if leftover:
                    logger.info("上下文 %s 有 %s 条剩余消息被丢弃", context_id, leftover)

    async def _publish_outbound(self, msg: OutboundMessage) -> None:
        """发布出站消息（由通道插件调用）。"""
        if self.event_bus:
            await self.event_bus.publish("agent.outbound", {
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "content": msg.content,
                "metadata": msg.metadata,
            })

    async def close_mcp(self) -> None:
        """关闭 MCP 连接。"""
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP 服务器 '%s' 清理错误（可忽略）", name)
        self._mcp_stacks.clear()

    def stop(self) -> None:
        """停止 Agent Loop。"""
        self._running = False
        logger.info("Agent loop 正在停止")

    async def process_direct(
        self,
        content: str,
        context_id: str = "default",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """直接处理消息并返回出站消息。"""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            context_id=context_id,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        context_id: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """处理单条入站消息，通过状态机驱动。"""
        # 刷新 provider 快照
        self._refresh_provider_snapshot()

        key = context_id or msg.context_id
        ctx = TurnContext(
            msg=msg,
            context_id=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )
        # 设置当前协程的 Trace ID，贯穿整个处理链路
        set_trace_id(ctx.trace_id)

        # 状态机驱动循环
        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"缺少状态处理器: {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(StateTraceEntry(
                    state=ctx.state, started_at=t0,
                    duration_ms=duration, event="", error="exception",
                ))
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(StateTraceEntry(
                state=ctx.state, started_at=t0,
                duration_ms=duration, event=event,
            ))
            logger.debug(
                "[turn %s] 状态 %s 耗时 %.1fms -> 事件 %s",
                ctx.turn_id, ctx.state.name, duration, event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] 状态 {ctx.state} 在事件 {event!r} 下无转换"
                )
            ctx.state = next_state

        logger.debug(
            "[turn %s] Turn 完成，经过 %s 个状态",
            ctx.turn_id, len(ctx.trace),
        )
        return ctx.outbound

    # --- 状态处理器 ---

    async def _state_restore(self, ctx: TurnContext) -> str:
        """恢复上下文，提取文档。"""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("处理来自 %s:%s 的消息: %s", msg.channel, msg.sender_id, preview)

        # 灵魂校验
        if self.event_bus:
            await self.event_bus.publish("agent.iteration_start", {
                "context_id": ctx.context_id,
                "turn_id": ctx.turn_id,
            })

        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        """压缩/合并上下文（MVP 阶段为空操作）。"""
        # MVP 阶段暂不实现自动压缩
        return "ok"

    async def _state_build(self, ctx: TurnContext) -> str:
        """构建初始消息列表。"""
        # 获取或创建上下文
        context = await self.context_manager.get_or_create(ctx.context_id)
        ctx.history = context.get_messages()

        # 受 token 预算限制的历史消息
        max_messages = 120
        max_tokens = self._replay_token_budget()
        if max_tokens > 0:
            system_prompt_len = 0  # 估算 system prompt token 数
            ctx.history = self._trim_history_by_tokens(
                ctx.history, max_tokens - system_prompt_len, max_messages,
            )
        else:
            ctx.history = ctx.history[-max_messages:]

        ctx.initial_messages = await self._build_initial_messages(ctx.msg, ctx.history)

        # 持久化用户消息
        current_content = ctx.msg.content
        if current_content and current_content.strip():
            context.add_message("user", current_content)
            ctx.user_persisted_early = True

        return "ok"

    async def _build_sandbox(self, user_id: str) -> Any | None:
        """根据用户上下文构建沙箱"""
        from nanobee.kernel.sandbox import ContextSandbox
        try:
            user_ctx = await self.context_manager.get_or_create(user_id)
            return ContextSandbox(user_ctx.context_root)
        except Exception:
            logger.debug("无法构建沙箱（非多租户模式）: %s", user_id)
            return None

    async def _state_run(self, ctx: TurnContext) -> str:
        """运行 Agent 迭代循环。"""
        sandbox = await self._build_sandbox(ctx.context_id)

        # 获取用户上下文
        user_ctx = await self.context_manager.get_or_create(ctx.context_id)

        # 让插件修改工具列表（在 ToolCollector 过滤之前）
        plugin_modified_tool_names = self._collect_plugin_tools(
            user_ctx, self.tools.tool_names,
        )

        # 构建 ToolCollector：用户白/黑名单 + 插件修改后的列表
        filtered_tool_names: list[str] | None = None
        try:
            from nanobee.kernel.tool_collector import ToolCollector
            collector = ToolCollector(
                tool_names=plugin_modified_tool_names,
                whitelist=user_ctx.whitelist,
                blacklist=user_ctx.blacklist,
            )
            if collector.has_restrictions:
                filtered_tool_names = collector.allowed_tools
        except Exception:
            logger.debug("构建 ToolCollector 失败，使用全部工具")

        result = await self._run_agent_loop(
            ctx.initial_messages,
            context_id=ctx.context_id,
            trace_id=ctx.trace_id,
            sandbox=sandbox,
            filtered_tool_names=filtered_tool_names,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            pending_queue=ctx.pending_queue,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """保存轮次结果到上下文。"""
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))

        # 保存 assistant 消息到上下文
        context = await self.context_manager.get_or_create(ctx.context_id)
        if ctx.final_content and ctx.final_content != EMPTY_FINAL_RESPONSE_MESSAGE:
            context.add_message("assistant", ctx.final_content)

        # 发射保存事件
        if self.event_bus:
            await self.event_bus.publish("agent.turn_saved", {
                "context_id": ctx.context_id,
                "turn_id": ctx.turn_id,
                "latency_ms": ctx.turn_latency_ms,
                "tools_used": ctx.tools_used,
            })

        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        """组装并返回出站消息。"""
        ctx.outbound = self._assemble_outbound(
            ctx.msg, ctx.final_content, ctx.all_messages,
            ctx.stop_reason, ctx.had_injections, ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    # --- 辅助方法 ---

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str | None,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """从轮次结果组装出站消息。"""
        content = final_content or EMPTY_FINAL_RESPONSE_MESSAGE

        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info("回复 %s: %s: %s", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=meta,
        )

    @staticmethod
    def _trim_history_by_tokens(
        history: list[dict[str, Any]],
        budget: int,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        """按 token 预算裁剪历史消息，保留最近的用户消息开头。"""
        if not history:
            return history
        if len(history) <= max_messages:
            return history

        # 简单裁剪：保留最近 max_messages 条
        return history[-max_messages:]

    # --- 模型预设管理 ---

    def _refresh_provider_snapshot(self) -> None:
        """刷新 provider 快照。"""
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("刷新 provider 配置失败")
            return
        if snapshot.signature == getattr(self, "_provider_signature", None):
            return
        self._apply_provider_snapshot(snapshot)

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """切换运行时的 provider/model。"""
        provider = snapshot.provider
        model = snapshot.model
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = snapshot.context_window_tokens
        self.runner.provider = provider
        self._provider_signature = snapshot.signature
        logger.info("运行时模型切换: %s -> %s", old_model, model)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """按名称解析预设并应用所有运行时 model 依赖。"""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _sync_subagent_runtime_limits(self) -> None:
        """保持子 Agent 运行时限制与可变的 Loop 设置对齐（MVP 不使用）。"""
        pass
