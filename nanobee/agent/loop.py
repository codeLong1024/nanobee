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
from nanobee.agent.mcp_manager import MCPManager
from nanobee.agent.preset_manager import ModelPresetManager
from nanobee.agent.messages import InboundMessage, OutboundMessage
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nanobee.utils.logger import logger


from nanobee.exceptions import LoopStateError
from nanobee.agent.hook import AgentHook, CompositeHook
from nanobee.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec, PluginHooks
from nanobee.agent.tools.registry import ToolRegistry, ToolPluginAdapter
from nanobee.providers.base import LLMProvider
from nanobee.providers.factory import ProviderSnapshot
from nanobee.utils.observability import generate_trace_id, set_trace_id
from nanobee.utils.document import extract_documents
from nanobee.utils.helpers import (
    build_assistant_message,
    build_runtime_context,
    estimate_prompt_tokens_chain,
    truncate_text,
)
from nanobee.utils.image_generation_intent import image_generation_prompt as image_gen_prompt_fn
from nanobee.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobee.config.schema import AgentDefaults, Config, ModelPresetConfig
    from nanobee.kernel.context_manager import ContextManager
    from nanobee.kernel.context_pipeline import ContextPipeline
    from nanobee.kernel.event_bus import EventBus
    from nanobee.kernel.plugin_manager import PluginManager
    from nanobee.kernel.skill_manager import SkillsLoader


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

    extra_hook: Any = None

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
        skill_manager: SkillManager | None = None,
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
        max_messages: int = 120,
        preset_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
    ) -> None:
        self.provider = provider
        self.workspace = workspace
        self.context_manager = context_manager
        self.context_pipeline = context_pipeline
        self.event_bus = event_bus
        self.plugin_manager = plugin_manager
        self.skill_manager = skill_manager
        from nanobee.kernel.router import ContextRouter
        self._router = router or ContextRouter()
        self.presets = ModelPresetManager(
            model_presets=model_presets,
            preset_snapshot_loader=preset_snapshot_loader,
            provider_snapshot_loader=provider_snapshot_loader,
        )

        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = max_tool_result_chars
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = tool_hint_max_length

        if model_preset:
            self.presets.set_active(model_preset)
            self.set_model_preset(model_preset, publish_update=False)

        self.tools = ToolRegistry()
        self._max_messages = max_messages
        self.runner = AgentRunner(provider)
        self._extra_hooks: list[AgentHook] = hooks or []

        self.mcp = MCPManager(mcp_servers)

        # 上下文级互斥锁：按用户粒度隔离并发
        # 同一 user_id 串行，不同 user_id 并行
        _max = int(os.environ.get("NANOBEE_MAX_CONCURRENT_REQUESTS", "3"))
        from nanobee.kernel.lock_manager import LockManager
        self._lock_manager = LockManager(max_concurrent=_max)

        # 消息分发器（延迟导入避免循环依赖）
        from nanobee.agent.dispatcher import MessageDispatcher
        self._dispatcher = MessageDispatcher(
            lock_manager=self._lock_manager,
            event_bus=self.event_bus,
            router=self._router,
            process_message_cb=self._process_message,
        )

        self._register_message_tool()
        self._register_plugin_tools()
        self._register_skill_tools()
        self._current_iteration: int = 0

    @classmethod
    def from_kernel(
        cls,
        provider: LLMProvider,
        workspace: Path,
        context_manager: Any,
        context_pipeline: Any,
        event_bus: Any,
        plugin_manager: Any,
        skill_manager: Any = None,
        router: Any = None,
        config: Config | dict | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """从 Kernel 子组件创建 AgentLoop。

        相比直接传入 ``kernel`` 对象的 duck-typing 方式，
        显式参数使契约更稳定，不依赖 kernel 内部的属性命名。

        Args:
            provider: LLM Provider 实例
            workspace: 工作目录
            context_manager: 上下文管理器
            context_pipeline: 上下文管线
            event_bus: 事件总线
            plugin_manager: 插件管理器
            skill_manager: 技能管理器
            router: 路由器（可选）
            config: 配置对象（Config 实例，用于读取 agents.defaults）
            **extra: 传递给 AgentLoop.__init__ 的额外参数
        """
        # 统一为 Config 对象（允许传入 dict 保持向后兼容）
        if isinstance(config, dict):
            from nanobee.config.schema import Config as _Config
            cfg = _Config(**config)
        else:
            cfg = config or Config()

        defaults = cfg.agents.defaults

        # 从配置中提取 max_iterations（如果未在 extra 中指定）
        if "max_iterations" not in extra:
            extra["max_iterations"] = defaults.max_iterations
        # 从配置中提取 max_messages（如果未在 extra 中指定）
        if "max_messages" not in extra:
            extra["max_messages"] = defaults.max_messages
        # 从配置中提取 context_window_tokens（如果未在 extra 中指定）
        if "context_window_tokens" not in extra:
            extra["context_window_tokens"] = defaults.context_window_tokens
        # 传递 MCP 服务器配置
        if "mcp_servers" not in extra and hasattr(cfg, "mcp_servers"):
            extra["mcp_servers"] = cfg.mcp_servers

        return cls(
            provider=provider,
            workspace=workspace,
            context_manager=context_manager,
            context_pipeline=context_pipeline,
            event_bus=event_bus,
            plugin_manager=plugin_manager,
            skill_manager=skill_manager,
            router=router,
            **extra,
        )

    def _register_message_tool(self) -> None:
        """注册 ``message`` 工具，让 LLM 可以结构化携带 media 参数发送文件。"""
        from nanobee.agent.tools.message import MessageTool
        self.tools.register(MessageTool())
        logger.info("message 工具已注册")

    def _register_plugin_tools(self) -> None:
        """从 PluginManager 注册工具插件到 ToolRegistry。

        注意：此方法需要在插件启用完成后调用（即在 Kernel.boot() 之后）。
        仅注册已启用的工具插件，跳过配置为禁用的插件。
        """
        if self.plugin_manager is None:
            return
        tool_plugins = self.plugin_manager.get_by_type("tool")
        if not tool_plugins:
            # 插件尚未加载，跳过注册（在 boot() 中会重新注册）
            return
        registered: list[str] = []
        self._throttled_tool_groups: dict[str, str] = {}
        self._exec_capable_tools: set[str] = set()
        self._file_edit_tools: set[str] = set()
        for plugin in tool_plugins:
            # 检查插件是否已启用
            if not self.plugin_manager.is_enabled(getattr(plugin, "name", "")):
                logger.debug("跳过未启用的工具插件: {}", getattr(plugin, "name", "unknown"))
                continue
            try:
                tool_defs = plugin.get_tools()
                for tool_def in tool_defs:
                    adapter = ToolPluginAdapter(plugin, tool_def)
                    self.tools.register(adapter)
                    registered.append(adapter.name)
                    # 收集需要节流的工具→组映射（插件声明了 throttle_group）
                    if plugin.metadata.throttle_group:
                        self._throttled_tool_groups[adapter.name] = plugin.metadata.throttle_group
                    # 收集具有命令执行能力的工具（用于工作区逃逸检测）
                    if plugin.metadata.exec_capable:
                        self._exec_capable_tools.add(adapter.name)
                    # 收集具有文件编辑能力的工具（用于进度追踪）
                    if plugin.metadata.file_edit_capability:
                        self._file_edit_tools.add(adapter.name)
            except Exception:
                logger.exception("注册工具插件 {name} 失败", name=getattr(plugin, "name", "unknown"))
        if self._throttled_tool_groups:
            logger.info("节流工具→组映射: {}", self._throttled_tool_groups)
        if self._exec_capable_tools:
            logger.info("可执行命令的工具: {}", self._exec_capable_tools)
        if self._file_edit_tools:
            logger.info("文件编辑工具: {}", self._file_edit_tools)
        logger.info("注册了 {count} 个工具插件: {plugins}", count=len(registered), plugins=registered)

        # 为沙箱注入 overlay 回退配置（skills/ → builtin skills）
        # 注：overlay 现已由 ContextSandbox.prefix_map 统一管理，
        # 在 _build_sandbox() 中构造时传入。

    def _register_skill_tools(self) -> None:
        """注册技能管理工具（不依赖插件系统，直接操作 SKILL.md）。

        这些工具让用户通过对话创建/编辑/删除自己的技能。
        使用 kernel.skill_manager 统一实例，避免路径分裂。
        """
        if self.skill_manager is None:
            return
        skill_loader: SkillsLoader = self.skill_manager
        from nanobee.agent.tools.skill_manager import ListSkillsTool
        self.tools.register(ListSkillsTool(skill_loader))
        logger.info("技能管理工具已注册")

    def _get_enabled_plugins(self) -> list[Any]:
        """获取所有已启用的插件。"""
        if self.plugin_manager is None:
            return []
        return self.plugin_manager.get_enabled_plugins()

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
                logger.exception("插件 {}.contribute_to_prompt 出错", getattr(plugin, "name", "?"))
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
                logger.exception("插件 {}.contribute_to_tools 出错", getattr(plugin, "name", "?"))
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
                logger.exception("插件 {}.on_message_completed 出错", getattr(plugin, "name", "?"))

    async def _connect_mcp(self) -> None:
        """连接配置的 MCP 服务器（委托给 MCPManager）。"""
        await self.mcp.connect(self.tools, default_cwd=str(self.workspace))

    @property
    def _pending_queues(self) -> dict[str, asyncio.Queue]:
        """待处理消息队列（委托给 MessageDispatcher，供 Kernel 访问）。"""
        return self._dispatcher.pending_queues

    async def _build_initial_messages(
        self,
        msg: InboundMessage,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构建 LLM 的初始消息列表。"""
        _b0 = time.perf_counter()
        from nanobee.kernel.context_pipeline import PromptBuildContext

        # 使用 ContextPipeline 构建系统提示词（含插件 Hook 贡献）
        pipeline_context = PromptBuildContext(
            context_id=msg.context_id,
            messages=history,
            system_prompt="",
        )

        # 获取用户上下文和已启用插件，用于 build_with_plugins()
        user_ctx = await self.context_manager.get_or_create(msg.context_id)
        _b1 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] get_or_create(inner): {:.0f}ms", (_b1 - _b0) * 1000)
        plugins = self._get_enabled_plugins()
        _b2 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] plugins_ready: {:.0f}ms", (_b2 - _b1) * 1000)
        system_prompt = await self.context_pipeline.build_with_plugins(
            pipeline_context, user_ctx, plugins,
        )
        _b3 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] build_with_plugins: {:.0f}ms (total {:.0f}ms)", (_b3 - _b2) * 1000, (_b3 - _b0) * 1000)

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
            # 估算当前上下文 token 用量，让 LLM 了解上下文窗口使用情况
            ctx_window = self.context_window_tokens or 0
            stats_str = ""
            if ctx_window > 0:
                _tok, _ = estimate_prompt_tokens_chain(
                    self.provider, self.model,
                    [{"role": "system", "content": system_prompt or ""}] + history,
                    self.tools.get_definitions(),
                )
                if _tok > 0:
                    _budget = max(ctx_window - 4096 - 1024, 1)
                    _pct = min(int((_tok / _budget) * 100), 999)
                    _tok_k = f"{_tok // 1000}k" if _tok >= 1000 else str(_tok)
                    _win_k = f"{ctx_window // 1000}k" if ctx_window >= 1000 else str(ctx_window)
                    stats_str = f"{len(history)} messages, {_tok_k}/{_win_k} tokens ({_pct}%)"

            # 注入 runtime context（时间、通道、会话信息、对话统计）
            runtime_ctx = build_runtime_context(
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                conversation_stats=stats_str,
            )
            messages.append({
                "role": "user",
                "content": f"{current_content}\n\n{runtime_ctx}",
            })

        return messages

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        *,
        context_id: str,
        channel: str = "",
        chat_id: str = "",
        sender_id: str = "",
        metadata: dict | None = None,
        trace_id: str | None = None,
        filtered_tool_names: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        extra_hook: Any = None,
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

        # 组装 hook：实例级 hooks（如 SDKCaptureHook）+ 请求级 extra_hook（如 StreamBridgeHook）
        # 使用请求级显式组合替代全局共享列表 append/remove，避免并发串台
        hooks: list[AgentHook] = list(self._extra_hooks or [])
        if extra_hook is not None:
            hooks.append(extra_hook)
        hook = CompositeHook(hooks) if hooks else AgentHook()

        # 构造插件 Hook 闭包列表（on_pre_invoke / on_post_invoke）
        plugin_hooks: PluginHooks | None = None
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
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            metadata=metadata or {},
            trace_id=trace_id or generate_trace_id(),
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            progress_callback=on_progress,
            stream_progress_deltas=on_stream is not None,
            retry_wait_callback=on_retry_wait,
            injection_callback=_drain_pending,
            filtered_tool_names=filtered_tool_names,
            plugin_hooks=plugin_hooks,
            throttled_tool_names=self._throttled_tool_groups,
            exec_capable_tools=self._exec_capable_tools,
            file_edit_tools=self._file_edit_tools,
        ))

        if result.stop_reason == "max_iterations":
            logger.warning("达到最大迭代次数 ({max_iter})", max_iter=self.max_iterations)
        elif result.stop_reason == "error":
            logger.error("LLM 返回错误: {error}", error=(result.final_content or "")[:200])

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
        """启动 Agent Loop（委托给 MessageDispatcher）。"""
        await self._dispatcher.run(self._consume_inbound, self._connect_mcp)

    async def _consume_inbound(self) -> InboundMessage:
        """消费入站消息（由通道插件调用）。"""
        raise NotImplementedError("子类或集成层需要实现此方法")

    async def _dispatch(self, msg: InboundMessage) -> None:
        """分发消息（委托给 MessageDispatcher）。"""
        await self._dispatcher._dispatch(msg)

    async def _publish_outbound(self, msg: OutboundMessage) -> None:
        """发布出站消息（委托给 MessageDispatcher）。"""
        await self._dispatcher._publish_outbound(msg)

    async def close_mcp(self) -> None:
        """关闭 MCP 连接（委托给 MCPManager）。"""
        await self.mcp.close()

    def stop(self) -> None:
        """停止 Agent Loop（委托给 MessageDispatcher）。"""
        self._dispatcher.stop()
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
        extra_hook: Any = None,
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
            extra_hook=extra_hook,
        )
        # 设置当前协程的 Trace ID，贯穿整个处理链路
        set_trace_id(ctx.trace_id)

        # 状态机驱动循环
        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise LoopStateError(f"缺少状态处理器: {ctx.state}")

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
                "[turn {turn_id}] 状态 {state} 耗时 {duration:.1f}ms -> 事件 {event}",
                turn_id=ctx.turn_id, state=ctx.state.name, duration=duration, event=event
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise LoopStateError(
                    f"[turn {ctx.turn_id}] 状态 {ctx.state} 在事件 {event!r} 下无转换"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {turn_id}] Turn 完成，经过 {states} 个状态",
            turn_id=ctx.turn_id, states=len(ctx.trace),
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

        # 灵魂校验
        if self.event_bus:
            await self.event_bus.publish("agent.iteration_start", {
                "context_id": ctx.context_id,
                "turn_id": ctx.turn_id,
            })

        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        """压缩/合并上下文 —— 裁剪过长的持久化历史。

        当持久化历史超过阈值时，裁剪至最大保留条数，
        防止历史无限增长导致 history.jsonl 膨胀。
        """
        user_ctx = await self.context_manager.get_or_create(ctx.context_id)
        messages = user_ctx.get_messages()

        trim_threshold = int(self._max_messages * 1.5)
        if len(messages) > trim_threshold:
            user_ctx.trim_to_last_n(self._max_messages)
            logger.info(
                "COMPACT: 裁剪持久化历史 %d → %d 条（用户 %s）",
                len(messages), self._max_messages, ctx.context_id,
            )

        return "ok"

    async def _state_build(self, ctx: TurnContext) -> str:
        """构建初始消息列表。"""
        _t0 = time.perf_counter()
        # 获取或创建上下文
        context = await self.context_manager.get_or_create(ctx.context_id)
        _t1 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] get_or_create: {:.0f}ms", (_t1 - _t0) * 1000)
        ctx.history = context.get_messages()

        # 安全阀：按条数上限暴力截断，防止历史无限增长。
        # LLM 通过 _memory skill + trim_history 工具自主管理记忆，
        # 框架不介入"保留哪些、裁多少"的策略决策。
        if len(ctx.history) > self._max_messages:
            ctx.history = ctx.history[-self._max_messages:]

        _t2 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] history_trim: {:.0f}ms (total {:.0f}ms)", (_t2 - _t1) * 1000, (_t2 - _t0) * 1000)
        ctx.initial_messages = await self._build_initial_messages(ctx.msg, ctx.history)
        _t3 = time.perf_counter()
        logger.debug("[BUILD-PROFILE] build_initial_messages: {:.0f}ms (total {:.0f}ms)", (_t3 - _t2) * 1000, (_t3 - _t0) * 1000)

        # 持久化用户消息
        current_content = ctx.msg.content
        if current_content and current_content.strip():
            context.add_message("user", current_content)
            ctx.user_persisted_early = True

        return "ok"

    async def _build_sandbox(self, user_id: str) -> Any | None:
        """根据用户上下文构建沙箱（含只读根白名单 + prefix_map 回退）"""
        from nanobee.kernel.sandbox import ContextSandbox
        try:
            user_ctx = await self.context_manager.get_or_create(user_id)
            # 内置技能目录 + 实例技能目录作为只读根加入沙箱，LLM 可读不可写
            read_only: list[Path | str] | None = None
            prefix_map: dict[str, Path | str] | None = None
            if self.skill_manager is not None:
                read_only = []
                # 内置技能目录
                builtin = self.skill_manager.builtin_dir
                if builtin is not None:
                    read_only.append(builtin)
                    builtin_skills = builtin / "skills"
                    if builtin_skills.is_dir():
                        prefix_map = {"skills/": builtin_skills}
                # 实例级技能目录（管理员配属，只读 —— 仅已启用的）
                enabled_dirs = self.skill_manager.get_enabled_instance_dirs()
                for d in enabled_dirs:
                    read_only.append(d)
                if not read_only:
                    read_only = None
            return ContextSandbox(
                user_ctx.context_root,
                read_only_roots=read_only,
                prefix_map=prefix_map,
            )
        except Exception:
            logger.debug("无法构建沙箱（非多租户模式）: {}", user_id)
            return None

    async def _state_run(self, ctx: TurnContext) -> str:
        """运行 Agent 迭代循环。"""
        sandbox = await self._build_sandbox(ctx.context_id)

        # 使用 ContextVar 绑定沙箱 + tmp + context_root + process_workspace + bwrap_ro_bind
        from nanobee.kernel.context_sandbox_var import (
            bind_bwrap_ro_bind, bind_context_root, bind_process_workspace,
            bind_sandbox, bind_tmp,
            reset_bwrap_ro_bind, reset_context_root, reset_process_workspace,
            reset_sandbox, reset_tmp,
        )
        _sandbox_token = bind_sandbox(sandbox) if sandbox else None

        # 绑定 per-request tmp 路径和 context_root（在获取 user_ctx 之后）
        user_ctx = await self.context_manager.get_or_create(ctx.context_id)
        _tmp_token = bind_tmp(user_ctx.tmp_dir)
        _ctx_root_token = bind_context_root(user_ctx.context_root)
        _process_ws_token = bind_process_workspace(user_ctx.work_dir)

        # 根据部署方 skills.enabled 推导 bwrap 额外只读挂载路径
        # 已启用实例技能目录在子进程（bwrap）中只读可见，
        # 确保 LLM 通过 execute_shell 执行技能脚本时路径可达
        _bwrap_ro_bind_token = None
        if self.skill_manager is not None:
            enabled_dirs = self.skill_manager.get_enabled_instance_dirs()
            if enabled_dirs:
                _bwrap_ro_bind_token = bind_bwrap_ro_bind(
                    [str(d) for d in enabled_dirs]
                )

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

        # 从 ctx.msg 提取通道上下文（用于工具插件 set_context 注入）
        msg = ctx.msg
        try:
            result = await self._run_agent_loop(
                ctx.initial_messages,
                context_id=ctx.context_id,
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                metadata=msg.metadata,
                trace_id=ctx.trace_id,
                filtered_tool_names=filtered_tool_names,
                on_progress=ctx.on_progress,
                on_stream=ctx.on_stream,
                on_stream_end=ctx.on_stream_end,
                pending_queue=ctx.pending_queue,
                extra_hook=ctx.extra_hook,
            )
        finally:
            if _sandbox_token is not None:
                reset_sandbox(_sandbox_token)
            reset_tmp(_tmp_token)
            reset_context_root(_ctx_root_token)
            reset_process_workspace(_process_ws_token)
            if _bwrap_ro_bind_token is not None:
                reset_bwrap_ro_bind(_bwrap_ro_bind_token)
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
            ctx.stop_reason, ctx.had_injections,
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
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """从轮次结果组装出站消息。

        扫描 ``all_msgs`` 中的 ``message`` 工具调用，提取 ``media`` 路径
        合并到出站消息中，让 LLM 可以结构化指定附件。
        """
        content = final_content or EMPTY_FINAL_RESPONSE_MESSAGE

        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info("回复 {}: {}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)
        # 写入客观终止原因（如 completed / max_iterations / error），
        # 通道据此决策：max_iterations 时卡片内容可能不完整，需追加通知。
        meta["stop_reason"] = stop_reason

        # 收集 message 工具调用中的 media 路径
        from nanobee.agent.tools.message import collect_message_tool_media
        tool_content, tool_media = collect_message_tool_media(all_msgs or [])
        existing_media = getattr(msg, "media", [])
        combined_media = existing_media + tool_media

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            media=combined_media,
            metadata=meta,
        )

    # --- 模型预设管理 ---

    def _refresh_provider_snapshot(self) -> None:
        """刷新 provider 快照，委托给 ModelPresetManager。"""
        snapshot = self.presets.check_and_get_snapshot()
        if snapshot is not None:
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
        self.presets.record_applied_snapshot(snapshot)
        logger.info("运行时模型切换: {} -> {}", old_model, model)

    @property
    def model_preset(self) -> str | None:
        return self.presets.active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """按名称解析预设并应用所有运行时 model 依赖。"""
        name = self.presets.normalize_name(name)
        snapshot = self.presets.build_snapshot(name, self.provider)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self.presets.set_active(name)

    def _sync_subagent_runtime_limits(self) -> None:
        """保持子 Agent 运行时限制与可变的 Loop 设置对齐（MVP 不使用）。"""
        pass
