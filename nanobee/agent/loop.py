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

from nanobee.agent.subagent import SubagentManager
from nanobee.agent.tools.subagent import ListSubagentsTool, SpawnSubagentTool
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
    truncate_text,
)
from nanobee.utils.image_generation_intent import image_generation_prompt as image_gen_prompt_fn
from nanobee.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobee.config.schema import AgentDefaults, Config, ModelPresetConfig
    from nanobee.kernel.context_manager import ContextManager
    from nanobee.kernel.context_pipeline import ContextPipeline
    from nanobee.events.event_bus import EventBus
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
    session_id: str
    state: TurnState
    turn_id: str

    # 对话历史（从 SessionManager 获取）
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
        session_manager: Any = None,
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
        _message_injector: Callable[[InboundMessage], None] | None = None,
        global_blacklist: list[str] | None = None,
    ) -> None:
        self.provider = provider
        self.workspace = workspace
        self._global_blacklist = global_blacklist or []
        self._message_injector = _message_injector  # 消息注入回调（供子代理 _injector 触发新 turn）
        self.context_manager = context_manager
        self.context_pipeline = context_pipeline
        self.session_manager = session_manager
        self.event_bus = event_bus
        self.plugin_manager = plugin_manager
        self.skill_manager = skill_manager
        self._router = router
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

        # 待处理消息队列（context_id -> asyncio.Queue），AgentLoop 直接持有
        # Kernel.handle_message 和 inject_message 均使用此字典进行中轮注入
        self._pending_queues: dict[str, asyncio.Queue] = {}

        # 子代理待注入结果缓存（context_id -> [content, ...]）
        self._pending_subagent_results: dict[str, list[str]] = {}

        # 先注册工具到 self.tools（注册顺序无关，self.tools 是同一个对象引用）
        self._register_message_tool()
        self.register_plugin_tools()
        self._register_skill_tools()

        # 初始化 SubagentManager（在工具注册之后，确保 tools_registry 已填充）
        self._subagent_manager = self._build_subagent_manager()

        self._register_subagent_tools()
        self._current_iteration: int = 0

        # 阻塞型 Hook 的待完成 Task 追踪（context_id → Task）
        # 同一 context_id 的下一次 dispatch 会等待这些 Task 完成
        self._pending_blockers: dict[str, asyncio.Task] = {}

        # 订阅子代理启动事件：立即通知用户，不经 LLM
        if self.event_bus:
            self.event_bus.subscribe("subagent.spawned", self._on_subagent_spawned)

    @classmethod
    def from_kernel(
        cls,
        provider: LLMProvider,
        workspace: Path,
        context_manager: Any,
        context_pipeline: Any,
        event_bus: Any,
        plugin_manager: Any,
        session_manager: Any = None,
        skill_manager: Any = None,
        router: Any = None,
        config: Config | dict | None = None,
        message_injector: Callable[[InboundMessage], None] | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """从 Kernel 子组件创建 AgentLoop。

        显式参数使契约更稳定，回调端口替代直接持有 Kernel 引用。

        Args:
            provider: LLM Provider 实例
            workspace: 工作目录
            context_manager: 上下文管理器
            context_pipeline: 上下文管线
            event_bus: 事件总线
            plugin_manager: 插件管理器
            session_manager: 会话管理器（可选）
            skill_manager: 技能管理器
            router: 路由器（可选）
            config: 配置对象（Config 实例，用于读取 agents.defaults）
            message_injector: 消息注入回调（同步 callable，无需 await）
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

        # 提取全局工具黑名单默认值（与 per-user blacklist 合并后用于 ToolCollector）
        global_blacklist = list(defaults.blacklist)

        return cls(
            provider=provider,
            workspace=workspace,
            context_manager=context_manager,
            context_pipeline=context_pipeline,
            session_manager=session_manager,
            event_bus=event_bus,
            plugin_manager=plugin_manager,
            skill_manager=skill_manager,
            router=router,
            _message_injector=message_injector,
            global_blacklist=global_blacklist,
            **extra,
        )

    # 公开 API：供 Kernel 调用的消息入口

    async def dispatch(
        self,
        msg: "InboundMessage",
        *,
        extra_hook: Any = None,
        on_progress: Any = None,
    ) -> "OutboundMessage | None":
        """公开消息入口：排队 → 加锁 → 处理 → 清理。

        Kernel 通过此方法派发消息，不再直接管理 AgentLoop 的内部队列和锁。

        Args:
            msg: 入站消息
            extra_hook: 可选的流式 Hook
            on_progress: 工具执行进度回调

        Returns:
            Agent 回复（OutboundMessage，含 .content 和 .media）
        """
        key = msg.context_id

        # 等待同一 ctx_id 的上一次 blocking hook task 完成
        # FIP：框架只提供"等待完成"机制，不决定"是否需要等"（由插件 block_next 声明）
        pending_blocker = self._pending_blockers.pop(key, None)
        if pending_blocker is not None and not pending_blocker.done():
            try:
                await pending_blocker
            except Exception:
                logger.warning("阻塞型 Hook 异常 (context={})，跳过继续", key)

        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[key] = pending

        try:
            async with self._lock_manager.acquire(key):
                return await self._process_message(
                    msg,
                    pending_queue=pending,
                    extra_hook=extra_hook,
                    on_progress=on_progress,
                )
        finally:
            queue = self._pending_queues.pop(key, None)
            if queue is not None:
                leftover = 0
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        leftover += 1
                    except asyncio.QueueEmpty:
                        break
                if leftover:
                    logger.info("上下文 {} 有 {} 条剩余消息被丢弃", key, leftover)

    def try_inject(self, msg: "InboundMessage") -> bool:
        """中轮注入消息到正在运行的 turn。

        Args:
            msg: 待注入的入站消息

        Returns:
            True 表示消息已放入队列，False 表示当前无活跃 turn 可注入
        """
        key = msg.context_id
        if key in self._pending_queues:
            try:
                self._pending_queues[key].put_nowait(msg)
                logger.debug("消息已中轮注入到上下文 {}", key)
                return True
            except asyncio.QueueFull:
                logger.warning("上下文 {} 待处理队列已满，丢弃注入消息", key)
                return False
        return False

    def _register_message_tool(self) -> None:
        """注册 ``message`` 工具，让 LLM 可以结构化携带 media 参数发送文件。"""
        from nanobee.agent.tools.message import MessageTool
        self.tools.register(MessageTool())
        logger.info("message 工具已注册")

    def register_plugin_tools(self) -> None:
        """从 PluginManager 注册工具插件到 ToolRegistry。

        公开 API：供 Kernel.boot() 在插件启用完成后调用。
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

    def _register_subagent_tools(self) -> None:
        """注册 subagent 相关工具给 LLM。"""
        if self._subagent_manager is None:
            logger.debug("SubagentManager 未初始化，跳过 subagent 工具注册")
            return
        self.tools.register(SpawnSubagentTool(self._subagent_manager))
        self.tools.register(ListSubagentsTool(self._subagent_manager))
        logger.info("subagent 工具已注册")

    async def _on_subagent_spawned(self, data: dict) -> None:
        """子代理启动事件处理：构建模板通知并立即推送给用户。

        不经 LLM 生成确认消息，直接通过 agent.outbound 事件发送。
        通道侧 on_enable 时已订阅 agent.outbound，此事件会自动路由。

        Args:
            data: subagent.spawned 事件载荷，包含 channel/chat_id/label/task/task_id。
        """
        from nanobee.utils.notifications import get_notification_content

        if not isinstance(data, dict):
            return

        channel = data.get("channel", "cli")
        chat_id = data.get("chat_id", "direct")
        if not channel or not chat_id:
            return

        content = get_notification_content(
            "subagent_spawned",
            label=data.get("label", "unknown"),
            task_id=data.get("task_id", ""),
            task_preview=data.get("task", "")[:100],
        )

        await self.event_bus.publish("agent.outbound", {
            "channel": channel,
            "chat_id": chat_id,
            "content": content,
            "metadata": {
                "notification_type": "system",
                "notification_kind": "subagent_spawned",
                "severity": "info",
            },
        })

    def _build_subagent_manager(self) -> SubagentManager:
        """创建 SubagentManager 实例（在 __init__ 末尾调用）。"""
        from nanobee.config.schema import AgentDefaults
        defaults = AgentDefaults()

        # 技能摘要构建器：使用 skill_manager 列出可用技能
        def _skills_summary(workspace: Path | None) -> str:
            if self.skill_manager is None:
                return ""
            all_skills = self.skill_manager.list_all_skills()
            return "\n".join(
                f"- {s.meta.name}: {s.meta.description}"
                for s in all_skills
            ) if all_skills else ""

        # 结果注入器：写入待注入缓存 + 主动触发新 turn
        # 对齐 nanobot bus.publish_inbound 模式：子代理完成时立即注入合成消息，
        # 触发新 Agent turn 处理结果，而非等待用户下一条消息。
        # 通过 kernel.inject_message() 统一入口，中轮注入时 put_nowait 非阻塞，
        # 新 turn 时 create_task 后台处理并通过 EventBus 发布结果。
        async def _injector(content: str, ctx_id: str, metadata: dict) -> None:
            # 写入待注入缓存（状态 BUILD 时排空，注入子代理结果到 LLM 上下文）
            self._pending_subagent_results.setdefault(ctx_id, []).append(content)
            # 创建合成消息主动触发新 Agent turn（对齐 nanobot 模式）
            channel = metadata.get("origin_channel", "system")
            chat_id = metadata.get("origin_chat_id", ctx_id)
            session_id = metadata.get("origin_session_id")
            trigger_msg = InboundMessage(
                channel=channel,
                sender_id=ctx_id,
                chat_id=chat_id,
                content="",  # 空内容，子代理结果由 _state_build 注入
                session_id_override=session_id,
                metadata={"_subagent_auto_trigger": True},
            )
            if self._message_injector is not None:
                self._message_injector(trigger_msg)
            else:
                logger.error("_injector: message_injector 未设置，无法注入子代理结果")

        return SubagentManager(
            provider=self.provider,
            workspace=self.workspace,
            model=self.model,
            tools_registry=self.tools,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            result_injector=_injector,
            skills_summary_builder=_skills_summary,
            event_bus=self.event_bus,
        )

    def _get_enabled_plugins(self) -> list[Any]:
        """获取所有已启用的插件。"""
        if self.plugin_manager is None:
            return []
        return self.plugin_manager.get_enabled_plugins()

    def _build_plugin_hooks(
        self,
        enabled_plugins: list[Any],
        user_ctx: Any,
    ) -> PluginHooks | None:
        """构造插件 Hook 闭包列表，按 hook_config priority 降序排序。

        FIP：读取 hook_config 元数据决定执行顺序，框架只读标记、不懂含义。
        block_next 仅适用于 on_message_completed（后台 fire-and-forget 模式），
        on_pre_invoke / on_post_invoke 为同步拦截器链，仅 priority 参与排序。

        Args:
            enabled_plugins: 已启用的插件列表
            user_ctx: 当前用户上下文

        Returns:
            PluginHooks 字典（pre_invoke/post_invoke 两个列表），无插件时返回 None
        """
        if not enabled_plugins:
            return None

        pre_invoke_entries: list[tuple[int, Any]] = []
        post_invoke_entries: list[tuple[int, Any]] = []
        for p in enabled_plugins:
            pre_cfg = p.hook_config.get("on_pre_invoke")
            pre_priority = pre_cfg.priority if pre_cfg else 10
            pre_invoke_entries.append((
                pre_priority,
                lambda name, args, _p=p, _ctx=user_ctx: _p.on_pre_invoke(_ctx, name, args),
            ))
            post_cfg = p.hook_config.get("on_post_invoke")
            post_priority = post_cfg.priority if post_cfg else 10
            post_invoke_entries.append((
                post_priority,
                lambda name, result, _p=p, _ctx=user_ctx: _p.on_post_invoke(_ctx, name, result),
            ))
        # 按 priority 降序排序（高优先级先执行）
        pre_invoke_entries.sort(key=lambda x: -x[0])
        post_invoke_entries.sort(key=lambda x: -x[0])
        return {
            "pre_invoke": [fn for _, fn in pre_invoke_entries],
            "post_invoke": [fn for _, fn in post_invoke_entries],
        }

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
        """FIP 合规的 Hook 调度器：按插件声明的元数据分组调度。

        框架职责（机制）：
        - 读 block_next → 分 blocking / non-blocking 两组
        - 读 priority   → 组内降序排序
        - non-blocking → create_task, 不跟踪
        - blocking     → create_task, 追踪到 _pending_blockers[context_id]

        插件职责（策略）：通过 plugin.toml [hooks.on_message_completed] 自行声明。

        Args:
            context_id: 用户上下文 ID
            messages: 本轮完整的消息列表
        """
        try:
            user_ctx = await self.context_manager.get_or_create(context_id)
        except Exception:
            logger.debug("获取用户上下文失败，跳过 on_message_completed 通知")
            return

        # 收集所有实现了 on_message_completed 的插件及其 Hook 元数据
        entries: list[tuple[int, bool, float, NanobeePlugin]] = []
        for plugin in self._get_enabled_plugins():
            cfg = plugin.hook_config.get("on_message_completed")
            priority = cfg.priority if cfg else 10
            block_next = cfg.block_next if cfg else False
            timeout = cfg.timeout if cfg else 0.0
            entries.append((priority, block_next, timeout, plugin))

        if not entries:
            return

        # 按 priority 降序排序
        entries.sort(key=lambda x: (-x[0], x[1]))

        # 分组：blocking vs non-blocking
        blocking = [(p, timeout, plg) for p, bn, timeout, plg in entries if bn]
        non_blocking = [(p, plg) for p, bn, timeout, plg in entries if not bn]

        # non-blocking 组：fire-and-forget（每个独立 create_task）
        # add_done_callback 防止 shutdown 时 CancelledError 产生 "never retrieved" 警告
        for _priority, plugin in non_blocking:
            task = asyncio.create_task(
                self._safe_notify_one(plugin, user_ctx, messages, context_id)
            )
            task.add_done_callback(lambda t: t.exception() if t.exception() else None)

        # blocking 组：顺序 await，超时跳过，整体放在 create_task 中不阻塞 LLM 响应
        if blocking:
            async def _blocking_group():
                for _priority, timeout, plugin in blocking:
                    try:
                        if timeout > 0:
                            await asyncio.wait_for(
                                self._safe_notify_one(plugin, user_ctx, messages, context_id),
                                timeout=timeout,
                            )
                        else:
                            await self._safe_notify_one(plugin, user_ctx, messages, context_id)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "阻塞型 Hook {}.on_message_completed 超时 ({:.1f}s) (context={})，跳过",
                            getattr(plugin, "name", "?"),
                            timeout,
                            context_id,
                        )

            task = asyncio.create_task(_blocking_group())
            self._pending_blockers[context_id] = task

    async def _safe_notify_one(
        self,
        plugin: "NanobeePlugin",
        user_ctx: Any,
        messages: list[dict[str, Any]],
        context_id: str,
    ) -> None:
        """安全调用单个插件的 on_message_completed，异常隔离。"""
        try:
            await plugin.on_message_completed(user_ctx, messages)
        except Exception:
            logger.exception(
                "插件 {}.on_message_completed 出错 (context={})",
                getattr(plugin, "name", "?"),
                context_id,
            )

    async def _connect_mcp(self) -> None:
        """连接配置的 MCP 服务器（委托给 MCPManager）。"""
        await self.mcp.connect(self.tools, default_cwd=str(self.workspace))

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
            # 注入 runtime context（时间、通道、会话信息 + 粗略 token 统计）
            runtime_ctx = build_runtime_context(
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                history=history,
                system_prompt=system_prompt,
                ctx_window=self.context_window_tokens or 0,
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
        session_id: str = "default",
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

        enabled_plugins = self._get_enabled_plugins()
        user_ctx_for_hooks = await self.context_manager.get_or_create(context_id)
        plugin_hooks = self._build_plugin_hooks(enabled_plugins, user_ctx_for_hooks)

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
            session_id=session_id,
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

        # 通知插件对话轮次已完成（后台执行，不阻塞主流程）
        # 注：原 event_bus.publish("agent.turn_completed") 已移除（2026-06-27），
        # 迁移到 on_message_completed Hook（详见 docs/plugin_development.md 事件系统与迁移指南）
        task = asyncio.create_task(
            self._notify_plugins_message_completed(context_id, result.messages)
        )
        task.add_done_callback(lambda t: t.exception() if t.exception() else None)

        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def close_mcp(self) -> None:
        """关闭 MCP 连接（委托给 MCPManager）。"""
        await self.mcp.close()

    def stop(self) -> None:
        """停止 Agent Loop。"""
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
        session_id = msg.session_id
        ctx = TurnContext(
            msg=msg,
            context_id=key,
            session_id=session_id,
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                duration = (time.perf_counter() - t0) * 1000
                logger.exception("状态 {} 处理器异常", ctx.state.name)
                ctx.trace.append(StateTraceEntry(
                    state=ctx.state, started_at=t0,
                    duration_ms=duration, event="ok", error=str(exc),
                ))
                # 统一错误恢复：填充 ctx 后跳到 RESPOND，绕过 SAVE 不污染历史
                if ctx.final_content is None or not ctx.final_content.strip():
                    ctx.final_content = (
                        f"抱歉，处理请求时发生内部错误。\n"
                        f"{type(exc).__name__}: {exc}"
                    )
                ctx.stop_reason = "error"
                ctx.tools_used = ctx.tools_used or []
                ctx.all_messages = ctx.all_messages or []
                ctx.had_injections = False
                # 防止无限循环：如果已是 RESPOND 状态仍失败则无法恢复
                if ctx.state == TurnState.RESPOND:
                    logger.error("RESPOND 状态处理器异常，无法恢复")
                    raise
                ctx.state = TurnState.RESPOND
                continue

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
        """压缩/合并上下文 —— 裁剪过长的会话历史。

        当会话历史超过阈值时，裁剪至最大保留条数，
        防止历史无限增长导致 JSONL 文件膨胀。
        """
        session = self.session_manager.get_or_create(ctx.context_id, ctx.session_id)
        trim_threshold = int(self._max_messages * 1.5)
        msg_count = len(session.messages)
        if msg_count > trim_threshold:
            session.trim_to_last_n(self._max_messages)
            self.session_manager.save(session)
            logger.info(
                "COMPACT: 裁剪会话历史 %d → %d 条（用户 %s，会话 %s）",
                msg_count, self._max_messages, ctx.context_id, ctx.session_id,
            )

        return "ok"

    async def _state_build(self, ctx: TurnContext) -> str:
        """构建初始消息列表。"""
        # 从 SessionManager 加载历史
        session = self.session_manager.get_or_create(ctx.context_id, ctx.session_id)
        ctx.history = session.messages

        # 安全阀：按条数上限暴力截断，防止历史无限增长。
        # LLM 通过 _memory skill + trim_history 工具自主管理记忆，
        # 框架不介入"保留哪些、裁多少"的策略决策。
        if len(ctx.history) > self._max_messages:
            ctx.history = ctx.history[-self._max_messages:]

        # 注入待处理的子代理结果到历史开头
        pending_results = self._pending_subagent_results.pop(ctx.context_id, [])
        if pending_results:
            for result in pending_results:
                ctx.history.append({"role": "user", "content": result})
            logger.info("注入了 {} 条待处理的子代理结果", len(pending_results))

        ctx.initial_messages = await self._build_initial_messages(ctx.msg, ctx.history)

        # 持久化用户消息到 session
        current_content = ctx.msg.content
        if current_content and current_content.strip():
            session = self.session_manager.get_or_create(ctx.context_id, ctx.session_id)
            session.add_message("user", current_content)
            self.session_manager.save(session)
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
                # 实例级技能目录（管理员配属，只读 —— 自动全量加载）
                enabled_dirs = self.skill_manager.get_instance_dirs()
                for d in enabled_dirs:
                    read_only.append(d)
                if not read_only:
                    read_only = None
            return ContextSandbox(
                user_ctx.context_root,
                read_only_roots=read_only,
                prefix_map=prefix_map,
                process_workspace=user_ctx.work_dir,
            )
        except Exception:
            logger.debug("无法构建沙箱（非多租户模式）: {}", user_id)
            return None

    async def _state_run(self, ctx: TurnContext) -> str:
        """运行 Agent 迭代循环。"""
        _t_state_run = time.perf_counter()
        logger.debug("[RUN] 开始 RUN 状态 (context_id={})", ctx.context_id)
        sandbox = await self._build_sandbox(ctx.context_id)

        # 使用 ContextVar 绑定沙箱 + tmp + context_root + process_workspace + bwrap_ro_bind + bwrap_rw_bind + request_context
        from nanobee.kernel.context_sandbox_var import (
            RequestContext,
            bind_bwrap_ro_bind, bind_bwrap_rw_bind,
            bind_context_root,
            bind_process_workspace, bind_request_context,
            bind_sandbox, bind_tmp,
            reset_bwrap_ro_bind, reset_bwrap_rw_bind,
            reset_context_root,
            reset_process_workspace, reset_request_context,
            reset_sandbox, reset_tmp,
        )
        _sandbox_token = bind_sandbox(sandbox) if sandbox else None

        # 绑定 per-request tmp 路径、context_root、进程工作区
        user_ctx = await self.context_manager.get_or_create(ctx.context_id)
        _tmp_token = bind_tmp(user_ctx.tmp_dir)
        _ctx_root_token = bind_context_root(user_ctx.context_root)
        _process_ws_token = bind_process_workspace(user_ctx.work_dir)
        # 统一绑定 per-turn 路由上下文（对齐 nanobot RequestContext 模式）
        _rctx_token = bind_request_context(RequestContext(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            context_id=ctx.context_id,
            session_id=ctx.session_id,
            metadata=ctx.msg.metadata,
        ))

        # 根据实例技能目录推导 bwrap 额外只读挂载路径
        # 实例技能目录在子进程（bwrap）中只读可见，
        # 确保 LLM 通过 execute_shell 执行技能脚本时路径可达
        _bwrap_ro_bind_token = None
        if self.skill_manager is not None:
            enabled_dirs = self.skill_manager.get_instance_dirs()
            if enabled_dirs:
                _bwrap_ro_bind_token = bind_bwrap_ro_bind(
                    [str(d) for d in enabled_dirs]
                )

        # 将用户 skills_dir 绑定为 bwrap 额外可读写挂载路径，
        # 让 execute_shell 在沙箱中创建/修改的技能目录持久化到真实文件系统
        _bwrap_rw_bind_token = bind_bwrap_rw_bind(
            [str(user_ctx.skills_dir)]
        )

        # 让插件修改工具列表（在 ToolCollector 过滤之前）
        plugin_modified_tool_names = self._collect_plugin_tools(
            user_ctx, self.tools.tool_names,
        )

        # 构建 ToolCollector：全局默认 + 用户级白/黑名单 + 插件修改后的列表
        filtered_tool_names: list[str] | None = None
        try:
            from nanobee.kernel.tool_collector import ToolCollector
            # 合并全局默认黑名单与用户级黑名单（去重，用户级优先）
            merged_blacklist = list(dict.fromkeys(self._global_blacklist + user_ctx.blacklist))
            collector = ToolCollector(
                tool_names=plugin_modified_tool_names,
                whitelist=user_ctx.whitelist,
                blacklist=merged_blacklist,
            )
            if collector.has_restrictions:
                filtered_tool_names = collector.allowed_tools
        except Exception:
            logger.debug("构建 ToolCollector 失败，使用全部工具")

        # 从 ctx.msg 提取通道上下文（用于工具插件 set_context 注入）
        msg = ctx.msg
        _t_runner = time.perf_counter()
        logger.debug(
            "[RUN] 调用 runner.run (model={}, messages={}, tools={})",
            self.model, len(ctx.initial_messages), len(self.tools.tool_names),
        )
        try:
            result = await self._run_agent_loop(
                ctx.initial_messages,
                context_id=ctx.context_id,
                session_id=ctx.session_id,
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
            reset_request_context(_rctx_token)
            if _bwrap_ro_bind_token is not None:
                reset_bwrap_ro_bind(_bwrap_ro_bind_token)
            reset_bwrap_rw_bind(_bwrap_rw_bind_token)
        _elapsed_runner = (time.perf_counter() - _t_runner) * 1000
        logger.debug("[RUN] runner.run 完成，耗时 {:.0f}ms", _elapsed_runner)
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """保存轮次结果到会话。"""
        # 跳过子代理主动触发的合成消息（_subagent_auto_trigger），
        # 这类消息仅用于触发新 turn，不应保存到会话历史。
        if ctx.msg.metadata.get("_subagent_auto_trigger"):
            return "ok"

        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))

        # 保存 assistant 消息到 session
        session = self.session_manager.get_or_create(ctx.context_id, ctx.session_id)
        if ctx.final_content and ctx.final_content != EMPTY_FINAL_RESPONSE_MESSAGE:
            session.add_message("assistant", ctx.final_content)
            self.session_manager.save(session)

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
