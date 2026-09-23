"""Agent Loop - 核心消息调度引擎。

核心保留：TurnState 状态机（RESTORE→BUILD→RUN→SAVE→RESPOND→DONE）、
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
from nanobee.agent.messages import InboundMessage
from nanobee.outbound import OutboundMessage, publish_outbound
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nanobee.utils.logger import logger

from nanobee.agent.subagent import SubagentManager
from nanobee.agent.tools.subagent import ListSubagentsTool, SpawnSubagentTool
from nanobee.exceptions import LoopStateError
from nanobee.agent.hook import AgentHook, CompositeHook
from nanobee.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec, PluginHooks
from nanobee.agent.specs import ExitReason, TurnLedger, TurnReport
from nanobee.agent.tools.registry import ToolRegistry, ToolPluginAdapter
from nanobee.providers.base import LLMProvider
from nanobee.providers.factory import ProviderSnapshot
from nanobee.utils.observability import generate_trace_id, is_valid_trace_id, set_trace_id
from nanobee.utils.document import extract_documents
from nanobee.utils.user_id import resolve_storage_key
from nanobee.utils.helpers import (
    build_assistant_message,
    build_runtime_context,
    find_legal_message_start,
    strip_runtime_context,
    truncate_text,
)
from nanobee.utils.image_generation_intent import image_generation_prompt as image_gen_prompt_fn
from nanobee.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from nanobee.utils.redact import normalize_error, redact_secrets

if TYPE_CHECKING:
    from nanobee.config.schema import AgentDefaults, Config, ModelPresetConfig
    from nanobee.kernel.context_manager import ContextManager
    from nanobee.kernel.context_pipeline import ContextPipeline
    from nanobee.events.event_bus import EventBus
    from nanobee.kernel.plugin_manager import PluginManager
    from nanobee.plugins.base import NanobeePlugin
    # 仅用于类型注解（文件已启用 future annotations），避免 agent 层对 session 层
    # 新增运行时依赖边
    from nanobee.session.session import Session


# 落盘截断标记：与面向模型的 truncate_text 默认后缀区分——回看历史时要能分辨
# "落盘时被收紧"与"模型侧被截断"（无声丢失会误导排障）。
_PERSIST_TRUNCATED_SUFFIX = "\n(persist truncated)"

# 悬尾修复占位文本：工具调用已声明但结果缺失（被守卫拦截 / turn 中断 / 崩溃恢复）
# 时落盘的事实陈述，不含策略语义。
_CANCELLED_TOOL_RESULT_CONTENT = "[tool call cancelled: turn interrupted before result]"


class TurnState(Enum):
    """状态机状态枚举。"""
    RESTORE = auto()
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
    exit_reason: str = ""
    error: str | None = None
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

    # turn 终态 report 幂等守卫：True 表示已结账（正常路径或兜底路径二选一），
    # 保证「每 turn 恰好一份终态 report」（Phase 2 终态保证）。
    turn_report_emitted: bool = False

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
        (TurnState.RESTORE, "ok"): TurnState.BUILD,
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
        persist_tool_traces: bool = False,
        persist_reasoning: bool = False,
        tool_result_persist_max_chars: int = 8192,
        tool_args_persist_max_chars: int = 8192,
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
        # 会话工具轨迹落盘（机制开关与落盘上界，策略数值全部配置化）
        self._persist_tool_traces = persist_tool_traces
        self._persist_reasoning = persist_reasoning
        self._tool_result_persist_max_chars = tool_result_persist_max_chars
        self._tool_args_persist_max_chars = tool_args_persist_max_chars
        self.runner = AgentRunner(provider)
        self._extra_hooks: list[AgentHook] = hooks or []

        self.mcp = MCPManager(mcp_servers)

        # 上下文级互斥锁：按用户粒度隔离并发
        # 同一 user_id 串行，不同 user_id 并行
        _max_concurrent = int(os.environ.get("NANOBEE_MAX_CONCURRENT_REQUESTS", "3"))
        from nanobee.kernel.lock_manager import LockManager
        self._lock_manager = LockManager(max_concurrent=_max_concurrent)

        # 待处理消息队列（context_id -> asyncio.Queue），AgentLoop 直接持有
        # Kernel.handle_message 和 inject_message 均使用此字典进行中轮注入
        self._pending_queues: dict[str, asyncio.Queue] = {}

        # 子代理待注入结果缓存（context_id -> [content, ...]）
        self._pending_subagent_results: dict[str, list[str]] = {}

        # 先注册工具到 self.tools（注册顺序无关，self.tools 是同一个对象引用）
        self._register_message_tool()
        self.register_plugin_tools()

        # 初始化 SubagentManager（在工具注册之后，确保 tools_registry 已填充）
        self._subagent_manager = self._build_subagent_manager()

        self._register_subagent_tools()
        self._current_iteration: int = 0

        # 阻塞型 Hook 的待完成 Task 追踪（context_id → Task）
        # 同一 context_id 的下一次 dispatch 会等待这些 Task 完成
        self._pending_blockers: dict[str, asyncio.Task] = {}

        # fire-and-forget Hook 任务登记（turn 结账 / started 通知 / 兜底终态）：
        # 完成后自清；kernel.shutdown 经 drain_hook_tasks 有界等待——审计落盘
        # 任务若随 loop 关闭被取消，对应 turn span 将永久丢失（Phase 2）。
        self._hook_tasks: set[asyncio.Task] = set()

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
        # 从配置中提取会话工具轨迹落盘项（如果未在 extra 中指定）
        if "persist_tool_traces" not in extra:
            extra["persist_tool_traces"] = defaults.persist_tool_traces
        if "persist_reasoning" not in extra:
            extra["persist_reasoning"] = defaults.persist_reasoning
        if "tool_result_persist_max_chars" not in extra:
            extra["tool_result_persist_max_chars"] = defaults.tool_result_persist_max_chars
        if "tool_args_persist_max_chars" not in extra:
            extra["tool_args_persist_max_chars"] = defaults.tool_args_persist_max_chars
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

        await publish_outbound(self.event_bus, OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={
                "notification_type": "system",
                "notification_kind": "subagent_spawned",
                "severity": "info",
            },
        ))

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
                lambda call_id, name, args, _p=p, _ctx=user_ctx: _p.on_pre_invoke(_ctx, call_id, name, args),
            ))
            post_cfg = p.hook_config.get("on_post_invoke")
            post_priority = post_cfg.priority if post_cfg else 10
            post_invoke_entries.append((
                post_priority,
                lambda call_id, name, result, _p=p, _ctx=user_ctx: _p.on_post_invoke(_ctx, call_id, name, result),
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
        report: TurnReport,
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
            report: turn 结账单（runner 账本 + loop 盖章，真值唯一来源）
        """
        try:
            user_ctx = await self.context_manager.get_or_create(context_id)
        except Exception:
            # 升级为 warning（评审 #1 复核项）：守卫拒绝不应静默——被拒
            # 轮次在审计中零留痕，warning 提供排障线索（只记长度不记原值）
            logger.warning(
                "获取用户上下文失败，跳过 on_message_completed 通知 (len={})",
                len(context_id),
            )
            return

        # 收集全部已启用插件及其 Hook 元数据（completed 维持全量调度，
        # 兼容覆写了方法但未在 plugin.toml 声明的既有插件；声明仅决定
        # priority / block_next / timeout，不影响是否被调度）
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

        # non-blocking 组：fire-and-forget（每个独立 create_task，登记供关停 drain）
        for _priority, plugin in non_blocking:
            task = asyncio.create_task(
                self._safe_notify_one(plugin, user_ctx, report, context_id)
            )
            self._track_hook_task(task)

        # blocking 组：顺序 await，超时跳过，整体放在 create_task 中不阻塞 LLM 响应
        if blocking:
            async def _blocking_group():
                for _priority, timeout, plugin in blocking:
                    try:
                        if timeout > 0:
                            await asyncio.wait_for(
                                self._safe_notify_one(plugin, user_ctx, report, context_id),
                                timeout=timeout,
                            )
                        else:
                            await self._safe_notify_one(plugin, user_ctx, report, context_id)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "阻塞型 Hook {}.on_message_completed 超时 ({:.1f}s) (context={})，跳过",
                            getattr(plugin, "name", "?"),
                            timeout,
                            context_id,
                        )

            task = asyncio.create_task(_blocking_group())
            self._pending_blockers[context_id] = task
            self._track_hook_task(task)

    async def _safe_notify_one(
        self,
        plugin: NanobeePlugin,
        user_ctx: Any,
        report: TurnReport,
        context_id: str,
    ) -> None:
        """安全调用单个插件的 on_message_completed，异常隔离。"""
        try:
            await plugin.on_message_completed(user_ctx, report)
        except Exception:
            logger.exception(
                "插件 {}.on_message_completed 出错 (context={})",
                getattr(plugin, "name", "?"),
                context_id,
            )

    async def _notify_plugins_message_started(
        self,
        context_id: str,
        message: str,
        turn_id: str,
    ) -> None:
        """FIP 合规的 on_message_started Hook 调度器。

        与 on_message_completed 的调度器同构，但恒为 non-blocking
        （turn 开始不应被插件阻塞，``block_next`` 元数据对本 Hook 无意义），
        仅按插件声明的 priority 降序入队 fire-and-forget 任务。

        Args:
            context_id: 用户上下文 ID
            message: 用户原始输入文本
            turn_id: turn 唯一标识（与 TurnReport.turn_id 同源，均为 trace_id）
        """
        try:
            user_ctx = await self.context_manager.get_or_create(context_id)
        except Exception:
            # 同 on_message_completed：守卫拒绝不静默（评审 #1 复核项）
            logger.warning(
                "获取用户上下文失败，跳过 on_message_started 通知 (len={})",
                len(context_id),
            )
            return

        # 声明才调度（评审 #6 拍板）：started 是新增 Hook，无历史兼容
        # 包袱，且 payload 携带用户原始输入——声明即能力，只投递给显式
        # 声明了 [hooks.on_message_started] 的插件（数据最小化）。
        # completed 维持全量调度以兼容未声明 Hook 的既有插件（见其注释）。
        entries: list[tuple[int, NanobeePlugin]] = []
        for plugin in self._get_enabled_plugins():
            cfg = plugin.hook_config.get("on_message_started")
            if cfg is None:
                continue
            entries.append((cfg.priority, plugin))

        if not entries:
            return

        # 按 priority 降序入队（事件循环 FIFO 保证同轮 started 先于 completed 消费）
        entries.sort(key=lambda x: -x[0])
        for _priority, plugin in entries:
            task = asyncio.create_task(
                self._safe_notify_started_one(plugin, user_ctx, message, turn_id, context_id)
            )
            self._track_hook_task(task)

    async def _safe_notify_started_one(
        self,
        plugin: NanobeePlugin,
        user_ctx: Any,
        message: str,
        turn_id: str,
        context_id: str,
    ) -> None:
        """安全调用单个插件的 on_message_started，异常隔离。"""
        try:
            await plugin.on_message_started(user_ctx, message, turn_id)
        except Exception:
            logger.exception(
                "插件 {}.on_message_started 出错 (context={})",
                getattr(plugin, "name", "?"),
                context_id,
            )

    def _track_hook_task(self, task: asyncio.Task) -> None:
        """登记 fire-and-forget Hook 任务：完成后自清，供关停 drain。

        登记即接管异常回收（done callback 中取出异常，避免
        "Task exception was never retrieved" 警告；插件内异常已由
        _safe_notify_* 记录日志）。

        Args:
            task: 待登记的后台任务。
        """
        self._hook_tasks.add(task)
        task.add_done_callback(self._on_hook_task_done)

    def _on_hook_task_done(self, task: asyncio.Task) -> None:
        """Hook 任务收口：从登记集合自清，并取走异常防警告。"""
        self._hook_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            task.exception()

    async def drain_hook_tasks(self, timeout_s: float) -> int:
        """排空在途 Hook 任务（turn 结账 / started 通知），**等到静默或超时**。

        评审 #3 修复：单次快照等待会漏掉等待期内由被等待任务派生的
        子任务（真正的审计落盘任务），导致关停后继续 unload 丢账。
        现改为 deadline 内循环：每轮重算登记集合中未完成任务并等待，
        直到集合为空（返回 0）或超时（返回仍未完成的任务数）。
        ``_track_hook_task`` 保证所有派生子任务必然进入登记集合，
        循环即收敛。

        关停链路在排空在途 turn 之后调用：审计落盘是 fire-and-forget
        任务，若随 loop 关闭被取消，对应 turn span 将永久丢失。

        Args:
            timeout_s: 排空总预算（秒）。超时后未完成的任务不取消、不阻塞
                关停流程，由调用方决定去留。

        Returns:
            超时后仍未完成的任务数。
        """
        deadline = time.monotonic() + timeout_s
        while True:
            pending = [t for t in self._hook_tasks if not t.done()]
            if not pending:
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return len(pending)
            await asyncio.wait(pending, timeout=remaining)

    async def _emit_turn_report(
        self,
        *,
        turn: TurnContext | None,
        context_id: str,
        trace_id: str,
        turn_started_at: float | None,
        result: Any = None,
        abandon_error: str | None = None,
    ) -> None:
        """turn 终态 report 的唯一出口（幂等）：每 turn 恰好一份。

        正常路径由 ``_run_agent_loop`` 在 runner 返回后调用；兜底路径由
        ``_process_message`` 的 finally 在 turn 未产出 runner 结果时调用
        （exit_reason=ABANDONED）。``turn_report_emitted`` 标志防止双发。

        Args:
            turn: 当前 TurnContext；None 表示直接调用 ``_run_agent_loop``
                的场景（无 turn 生命周期，跳过幂等登记）。
            context_id: 用户上下文 ID。
            trace_id: turn 身份（turn_id，W3C trace id）。
            turn_started_at: dispatch 墙钟（None 时退化当前时刻）。
            result: runner 结果；None 时合成 ABANDONED 兜底终态。
            abandon_error: 兜底终态的诊断串。
        """
        if turn is not None:
            if turn.turn_report_emitted:
                return
            turn.turn_report_emitted = True

        turn_ended_iso = datetime.now().astimezone().isoformat()
        if result is not None:
            turn_started_iso = (
                datetime.fromtimestamp(turn_started_at).astimezone().isoformat()
                if turn_started_at is not None
                else datetime.now().astimezone().isoformat()
            )
            report = TurnReport(
                turn_id=trace_id,
                turn_started_at=turn_started_iso,
                ledger=result.ledger,
                turn_ended_at=turn_ended_iso,
                messages_window=result.messages[result.ledger.turn_input_index:],
            )
        else:
            # 兜底终态：runner 未返回（状态机异常/取消/关停排空）。账本仅含
            # 窗口锚点与退出语义；error 恒非 None，避免消费者把兜底误判为
            # 成功；消息窗口仅含输入侧（无 runner 结果侧）。
            initial_messages = getattr(turn, "initial_messages", None) or []
            window_start = max(len(initial_messages) - 1, 0)
            report = TurnReport(
                turn_id=trace_id,
                turn_started_at=(
                    datetime.fromtimestamp(turn.turn_wall_started_at).astimezone().isoformat()
                    if turn is not None
                    else turn_ended_iso
                ),
                ledger=TurnLedger(
                    turn_input_index=window_start,
                    exit_reason=ExitReason.ABANDONED.value,
                    error=abandon_error or "turn abandoned without runner result",
                ),
                turn_ended_at=turn_ended_iso,
                messages_window=list(initial_messages[window_start:]),
            )

        # 通知插件对话轮次已完成（后台执行，不阻塞主流程），并登记供关停 drain
        task = asyncio.create_task(
            self._notify_plugins_message_completed(context_id, report)
        )
        self._track_hook_task(task)

    async def connect_mcp(self) -> None:
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
        else:
            # 契约告警：TurnLedger.turn_input_index 以"末元素为本轮用户输入"为
            # 锚点（见 specs.TurnLedger docstring），空输入不应进入 turn——
            # 通道层应已过滤；此处兜底告警，防止锚点静默漂移到历史末条。
            logger.warning(
                "[TURN] 本轮未产生用户消息（content 为空），窗口锚点可能漂移 "
                "(context={})",
                msg.context_id,
            )

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
        turn_started_at: float | None = None,
        turn: TurnContext | None = None,
        filtered_tool_names: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        extra_hook: Any = None,
    ) -> tuple[str | None, list[str], list[dict], str, str | None, bool]:
        """运行 Agent 迭代循环（LLM 调用 + 工具执行）。

        Returns (final_content, tools_used, messages, exit_reason, error, had_injections)。
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

        # 边界归一化：外部传入的 trace_id 非法时静默重新生成，不污染日志串联。
        # 归一后的值同时作为 turn 身份（TurnReport.turn_id），保证全链路单一 ID。
        effective_trace_id = trace_id if is_valid_trace_id(trace_id) else generate_trace_id()
        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            hook=hook,
            concurrent_tools=True,
            workspace=self.workspace,
            context_id=context_id,
            session_id=session_id,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            metadata=metadata or {},
            trace_id=effective_trace_id,
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

        if result.exit_reason == ExitReason.MAX_ITERATIONS:
            logger.warning("达到最大迭代次数 ({max_iter})", max_iter=self.max_iterations)
        elif result.error is not None:
            logger.error("LLM 返回错误: {error}", error=result.error[:200])

        # 结账：loop 盖章（turn 身份 + dispatch 时刻）合成 TurnReport 交给插件。
        # runner 拥有核心窗口事实（ledger），loop 拥有 turn 生命周期（started_at）。
        # 消息窗口为切片浅拷贝（result.messages 为最终态，runner 不再修改）。
        # 幂等出口：兜底终态（ABANDONED）由 _process_message 的 finally 走同一
        # 方法，turn_report_emitted 标志保证每 turn 恰好一份（Phase 2 终态保证）。
        # 注：原 event_bus.publish("agent.turn_completed") 已移除（2026-06-27），
        # 迁移到 on_message_completed Hook（详见 docs/plugin_development.md）。
        await self._emit_turn_report(
            turn=turn,
            context_id=context_id,
            trace_id=effective_trace_id,
            turn_started_at=turn_started_at,
            result=result,
        )

        return (
            result.final_content,
            result.tools_used,
            result.messages,
            result.exit_reason.value,
            result.error,
            result.had_injections,
        )

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
        await self.connect_mcp()
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

        # 存储键出生点归一（评审 #1/#4）：context_id 参数是路由键（含通道
        # 前缀如 "dingtalk:xxx"，直接落盘会被白名单拒绝或越界写），在此
        # 归一为存储键；msg.context_id 已在 InboundMessage.context_id 属性
        # 完成归一。出站投递仍用 msg.chat_id（原路由值），路由不受影响。
        key = resolve_storage_key(context_id) if context_id else msg.context_id
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

        # 外层绑定 context_root（评审 #2 修复）：兜底 ABANDONED 结账任务在
        # 最外层 finally 中创建，此时 _state_run 的内层绑定已复位——若不在
        # 外层补绑，审计将回退 /tmp 进程级目录（可预测、跨实例共享、不防
        # 符号链接）。ContextVar.reset(token) 恢复 set 之前的值，故 _state_run
        # 的内层 bind/reset 与本外层绑定天然兼容（复位恢复外层值），零改动。
        # get_or_create 失败（如 user_id 非法）时不绑定，保持 /tmp 回退——
        # 该场景结账通知同样会被 ContextManager 守卫拦下，无有效审计可写。
        from nanobee.kernel.context_sandbox_var import (
            bind_context_root,
            reset_context_root,
        )
        _outer_ctx_root_token: Any = None
        try:
            outer_user_ctx = await self.context_manager.get_or_create(key)
            if outer_user_ctx.context_root is not None:
                _outer_ctx_root_token = bind_context_root(outer_user_ctx.context_root)
        except Exception:
            logger.debug(
                "[turn {turn_id}] 外层绑定 context_root 失败，兜底审计走回退目录",
                turn_id=ctx.turn_id,
            )

        # 通知插件对话轮次已开始（后台执行，不阻塞主流程）
        # 与 on_message_completed 配对，为插件提供真实 turn 起点（如审计 span 计时）。
        # turn_id 与 TurnReport.turn_id 同源（ctx.trace_id），供插件关联起止。
        task = asyncio.create_task(
            self._notify_plugins_message_started(key, msg.content, ctx.trace_id)
        )
        self._track_hook_task(task)

        # 状态机驱动循环（Phase 2 终态保证：任何退出路径都保证恰好一份终态
        # report——正常路径在 _run_agent_loop 内结账，取消/异常路径由 finally
        # 兜底补发 ABANDONED；CancelledError 原样传播，兜底在取消路径内用
        # create_task 登记，不在取消路径 await，避免二次取消打断落账）
        abandon_reason = ""
        try:
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
                    # 兜底错误恢复：runner.run() 已把内部异常折叠进 result.error 正常返回，
                    # 此分支仅覆盖 runner 之外的意外异常。填充 error 后跳到 RESPOND（绕过 SAVE，
                    # 避免异常半途状态写入历史），错误经系统通知路径（fail_card）下发。
                    # error 保持纯技术诊断串：致歉文案由通知模板统一包装，避免"双重道歉"。
                    ctx.error = normalize_error(exc)
                    ctx.final_content = None
                    ctx.exit_reason = ExitReason.COMPLETED.value
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
        except asyncio.CancelledError:
            abandon_reason = "turn cancelled before completion"
            raise
        finally:
            if not ctx.turn_report_emitted:
                if not abandon_reason:
                    abandon_reason = "turn aborted by state machine failure"
                self._track_hook_task(asyncio.create_task(
                    self._emit_turn_report(
                        turn=ctx,
                        context_id=ctx.context_id,
                        trace_id=ctx.trace_id,
                        turn_started_at=ctx.turn_wall_started_at,
                        result=None,
                        # 真实诊断优先（评审 #7）：ctx.error 由状态机异常分支
                        # 填充（含异常类名与消息），取消路径为 None 时退回
                        # 通用 abandon_reason。
                        abandon_error=ctx.error or abandon_reason,
                    )
                ))
            # 外层复位：必须在兜底任务创建**之后**（create_task 复制创建
            # 时刻的上下文，先复位会让 ABANDONED 结账拿到 root=None）。
            if _outer_ctx_root_token is not None:
                reset_context_root(_outer_ctx_root_token)

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

    async def _state_build(self, ctx: TurnContext) -> str:
        """构建初始消息列表。"""
        # 从 SessionManager 加载历史（fresh_session 时走隔离空会话）
        session = self.session_manager.get_or_create(ctx.context_id, self._resolve_session_id(ctx))

        # 安全阀：当会话历史超限时，硬截断 session 本体并回写。
        # 这是框架唯一的历史截断保障（机制），不涉及保留策略。
        # LLM 通过 memory skill + trim_history/consolidate_history 工具自主管理记忆。
        msg_count = len(session.messages)
        if msg_count > self._max_messages:
            session.messages = session.messages[-self._max_messages:]
            logger.warning(
                "BUILD 安全阀：裁剪会话历史 %d → %d 条（用户 %s，会话 %s）",
                msg_count, len(session.messages), ctx.context_id, ctx.session_id,
            )

        self._repair_replay_window_head(ctx, session)

        # ctx.history 必须在截断之后赋值，确保与 session.messages 指向同一 list
        ctx.history = session.messages

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
            session = self.session_manager.get_or_create(ctx.context_id, self._resolve_session_id(ctx))
            session.add_message("user", current_content)
            self.session_manager.save(session)
            ctx.user_persisted_early = True

        return "ok"

    def _repair_replay_window_head(self, ctx: TurnContext, session: Session) -> None:
        """回放合法性自愈（窗口头部）：丢弃声明已被截掉的孤儿协议消息。

        截断（安全阀切片 / memory skill 的 trim_history）可能把 assistant 声明切掉
        却留下 ``role:"tool"`` 结果，这类条目原样发给 provider 会协议报错。此处
        对齐到合法起点并修正在内存中的回放窗口；纯文本历史（无协议消息）天然
        no-op。

        注意：本方法**只改内存**（缓存对象），磁盘上的会话文件在下一次成功
        ``session_manager.save`` 时收敛——正常轮次由 BUILD 的 user 提前落盘随即
        写回，异常跳过 SAVE 的轮次则留待下次。

        Args:
            ctx: 当前 turn 上下文（仅用于日志标识）。
            session: 待自愈的会话（其 ``messages`` 可能被就地替换）。
        """
        legal_start = find_legal_message_start(session.messages)
        if not legal_start:
            return
        logger.warning(
            f"BUILD 回放自愈：丢弃窗口头部 {legal_start} 条孤儿协议消息"
            f"（用户 {ctx.context_id}，会话 {ctx.session_id}）"
        )
        session.messages = session.messages[legal_start:]

    # 隔离会话命名空间前缀（机制保留名，避免与 channel:chat_id 派生值冲突）
    _FRESH_SESSION_PREFIX = "__fresh__:"

    def _resolve_session_id(self, ctx: TurnContext) -> str:
        """解析本次 turn 实际使用的会话 ID。

        声明式无历史机制：当 ``ctx.msg.fresh_session`` 为 True 时，
        返回独立隔离空会话 ID（不加载该用户历史），否则返回原会话 ID。
        框架只读标记，不关心调用方为何声明（框架无知论）。
        turn_id 后缀保证每次触发都是全新空会话，绝不残留上次执行的对话。

        Args:
            ctx: 当前 turn 上下文

        Returns:
            实际使用的会话 ID
        """
        if ctx.msg.fresh_session:
            return f"{self._FRESH_SESSION_PREFIX}{ctx.session_id}:{ctx.turn_id}"
        return ctx.session_id

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
                turn_started_at=ctx.turn_wall_started_at,
                turn=ctx,
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
        final_content, tools_used, all_msgs, exit_reason, error, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.exit_reason = exit_reason
        ctx.error = error
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """保存轮次结果到会话。"""
        # 跳过子代理主动触发的合成消息（_subagent_auto_trigger），
        # 这类消息仅用于触发新 turn，不应保存到会话历史。
        if ctx.msg.metadata.get("_subagent_auto_trigger"):
            return "ok"

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))

        # 保存 assistant 消息到 session
        # 错误时 final_content=None（失败语义由 ctx.error 承载），不保存占位符到历史。
        # fresh_session 是一次性隔离会话：turn 结束后必须立即回收，
        # 防止 cron 长期运行累积 __fresh__:* 孤儿会话（JSONL 文件 + _cache 膨胀）。
        # 用 try/finally 保证回收：即使 save 或后续事件发布抛异常，fresh 会话也不残留。
        resolved_session_id = self._resolve_session_id(ctx)
        session = self.session_manager.get_or_create(ctx.context_id, resolved_session_id)
        try:
            # 本轮执行轨迹（tool_calls 声明 / tool 结果）先落盘，再落终文本：
            # 失败/中断轮也要留痕，否则历史里只剩"宣称完成"的终文本，
            # 回放时看不到"先调工具才宣称完成"的因果链。
            saved_count = 0
            final_text_persisted = False
            if self._persist_tool_traces:
                persisted, final_text_persisted = self._persist_tool_trace_increment(ctx, session)
                saved_count += persisted
            # 终文本：开关关闭、或增量里找不到对应条目（异常形态）时由既有路径补落
            if ctx.final_content and not final_text_persisted:
                session.add_message("assistant", ctx.final_content)
                saved_count += 1
            # 有增量才写盘：无终文本且无轨迹的轮次保持"不落盘"旧语义
            if saved_count:
                self.session_manager.save(session)

            # 发射保存事件
            if self.event_bus:
                await self.event_bus.publish("agent.turn_saved", {
                    "context_id": ctx.context_id,
                    "turn_id": ctx.turn_id,
                    "latency_ms": ctx.turn_latency_ms,
                    "tools_used": ctx.tools_used,
                })
        finally:
            if ctx.msg.fresh_session:
                self.session_manager.delete(ctx.context_id, resolved_session_id)

        return "ok"

    def _persist_tool_trace_increment(
        self,
        ctx: TurnContext,
        session: Session,
    ) -> tuple[int, bool]:
        """落盘本轮新增的协议消息（assistant(tool_calls) 声明 / tool 结果）。

        增量定义：``ctx.all_messages`` 中位于 ``ctx.initial_messages`` 之后的片段。
        锚点与 ``TurnLedger.turn_input_index`` 同源契约（``len(initial_messages) - 1``
        即本轮用户输入，见 specs.TurnLedger docstring），其后一位即本轮新增起点；
        本轮用户消息已在 BUILD 提前落盘（崩溃安全），此处不重复落。

        配对校验 / 悬尾占位 / 清洗 / 脱敏全部在落盘出生点完成，逐条经
        :meth:`Session.add_protocol_message` 入账。只读取 ctx，绝不改写
        ``ctx.final_content`` / ``ctx.all_messages``（RESPOND 独立消费二者）。

        Args:
            ctx: 当前 turn 上下文。
            session: 目标会话。

        Returns:
            ``(实际落盘条数, 终文本是否已在本方法内落盘)``。条数为 0 表示无增量或
            切片前提不满足（回退旧口径）；终文本标志供 :meth:`_state_save` 判断是否
            仍需补落终文本（同一条终文本只落一次）。
        """
        increment = self._turn_increment(ctx)
        if not increment:
            return 0, False

        # 配对校验种子（照抄 nanobot `_save_turn`）：以已落盘历史为基准，
        # 兼容崩溃恢复后补落的孤儿结果与跨 turn 的重复落盘。
        declared = self._declared_tool_call_ids(session.messages)
        fulfilled = self._fulfilled_tool_call_ids(session.messages)
        # 增量内已携带结果的 call id：悬尾判定必须看整段增量——结果总在声明之后
        # 出现，逐条处理会把"还没轮到的结果"误判为缺失而多落一条占位。
        increment_result_ids = self._fulfilled_tool_call_ids(increment)
        # 终文本条目（内容等于 ctx.final_content 的最后一条纯文本 assistant）
        skip_index = self._final_text_index(increment, ctx.final_content)

        persisted = 0
        final_text_persisted = False
        for index, message in enumerate(increment):
            if index == skip_index:
                # 终文本**按增量原位落盘**：保持与 runner 内部真实顺序一致
                # （max_iterations 出口会先追加终文本、再追加注入的 user 消息）
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    session.add_message("assistant", content)
                    persisted += 1
                    final_text_persisted = True
                continue
            role = message.get("role")
            if role == "tool":
                if self._persist_tool_result(session, message, declared, fulfilled):
                    persisted += 1
            elif role == "assistant":
                persisted += self._persist_assistant_trace(
                    session, message, declared, fulfilled, increment_result_ids,
                )
            elif role == "user":
                # 轮内注入的 user 条目（drain）此前从未落盘，如实入账；
                # 防御性剥离运行时尾注（普通 user 消息在 BUILD 已按原文落盘）。
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                stripped = strip_runtime_context(content)
                if stripped.strip():
                    session.add_message("user", stripped)
                    persisted += 1
        return persisted, final_text_persisted

    def _persist_assistant_trace(
        self,
        session: Session,
        message: dict[str, Any],
        declared: set[str],
        fulfilled: set[str],
        increment_result_ids: set[str],
    ) -> int:
        """落盘一条 assistant 增量消息，返回落盘条数（含悬尾占位）。"""
        entry = dict(message)
        raw_calls = entry.get("tool_calls")
        if not self._persist_reasoning:
            # 思维链是临时推理内容，默认不落盘（token 大头，联调时可用开关保留）
            entry.pop("reasoning_content", None)
            entry.pop("thinking_blocks", None)

        if not raw_calls:
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                # 空 assistant 会污染会话上下文（与 nanobot 同规则）
                return 0
            session.add_message("assistant", content)
            return 1

        calls = self._clean_tool_calls(raw_calls)
        if not calls:
            return 0
        entry["tool_calls"] = calls
        session.add_protocol_message(entry)
        persisted = 1
        declared.update(str(call["id"]) for call in calls)

        # 悬尾修复（nanobot 未覆盖的缺口）：声明已落盘、但增量与历史都没有结果的
        # 调用，合成取消占位结果一并落盘。否则会话文件里只剩"宣称调用过"而无结果，
        # 正是本方案要消灭的那种历史断档（工具被守卫拦截 / turn 中断 / 崩溃恢复）。
        for call in calls:
            call_id = str(call["id"])
            if call_id in fulfilled or call_id in increment_result_ids:
                continue
            placeholder: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _CANCELLED_TOOL_RESULT_CONTENT,
            }
            tool_name = self._tool_name_of(call)
            if tool_name:
                # 部分 provider 对 tool 消息的 name 非空有隐含要求：缺失时省略该键
                placeholder["name"] = tool_name
            session.add_protocol_message(placeholder)
            fulfilled.add(call_id)
            persisted += 1
        return persisted

    def _persist_tool_result(
        self,
        session: Session,
        message: dict[str, Any],
        declared: set[str],
        fulfilled: set[str],
    ) -> bool:
        """校验并落盘一条 tool 结果；非法（缺 id / 未声明 / 重复）则丢弃并告警。"""
        entry = dict(message)
        raw_id = entry.get("tool_call_id")
        call_id = str(raw_id) if raw_id else ""
        if not call_id or call_id not in declared or call_id in fulfilled:
            # 未声明/重复的工具结果会破坏后续 provider 请求（nanobot 同规则）
            logger.warning(
                f"轨迹落盘丢弃非法工具结果 {call_id or '(missing id)'}"
                f"（缺 id / 未声明 / 重复），会话 {session.session_id}"
            )
            return False
        # 归一为 str 后写盘：校验值与落盘值同源（add_protocol_message 只接受非空 str）
        entry["tool_call_id"] = call_id
        fulfilled.add(call_id)
        content = entry.get("content")
        if isinstance(content, str):
            # 先脱敏再截断：顺序颠倒会被截断切断密钥形态而漏出半截凭证
            entry["content"] = truncate_text(
                redact_secrets(content),
                self._tool_result_persist_max_chars,
                suffix=_PERSIST_TRUNCATED_SUFFIX,
            )
        session.add_protocol_message(entry)
        return True

    def _clean_tool_calls(self, calls: list[Any]) -> list[dict[str, Any]]:
        """清洗待落盘的 tool_calls：丢弃无 id 项，参数脱敏 + 限长（浅拷贝，不回改入参）。"""
        cleaned: list[dict[str, Any]] = []
        for raw_call in calls:
            if not isinstance(raw_call, dict) or not raw_call.get("id"):
                continue
            call = dict(raw_call)
            # id 归一为 str：与 declared/fulfilled 集合的口径一致（避免 int id 半截匹配）
            call["id"] = str(call["id"])
            function = call.get("function")
            if isinstance(function, dict):
                cleaned_function = dict(function)
                arguments = cleaned_function.get("arguments")
                if isinstance(arguments, str):
                    cleaned_function["arguments"] = truncate_text(
                        redact_secrets(arguments),
                        self._tool_args_persist_max_chars,
                        suffix=_PERSIST_TRUNCATED_SUFFIX,
                    )
                call["function"] = cleaned_function
            cleaned.append(call)
        return cleaned

    @staticmethod
    def _declared_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
        """收集消息列表中 assistant 已声明的 tool call id。"""
        declared: set[str] = set()
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for raw_call in message.get("tool_calls") or []:
                if isinstance(raw_call, dict) and raw_call.get("id"):
                    declared.add(str(raw_call["id"]))
        return declared

    @staticmethod
    def _fulfilled_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
        """收集消息列表中已有结果的 tool call id。"""
        fulfilled: set[str] = set()
        for message in messages:
            if message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            if call_id:
                fulfilled.add(str(call_id))
        return fulfilled

    @staticmethod
    def _final_text_index(
        increment: list[dict[str, Any]],
        final_content: str | None,
    ) -> int | None:
        """定位增量中"已是终文本"的 assistant 条目下标（该条由既有路径落盘）。

        取最后一个内容等于 ``final_content`` 的纯文本 assistant：runner 的终文本
        必定是增量中最后一条匹配项（``_append_final_message`` 落在末尾）。
        """
        if not final_content:
            return None
        for index in range(len(increment) - 1, -1, -1):
            message = increment[index]
            if message.get("role") != "assistant" or message.get("tool_calls"):
                continue
            if message.get("content") == final_content:
                return index
        return None

    @staticmethod
    def _tool_name_of(call: dict[str, Any]) -> str:
        """提取工具名（缺失时返回空串，协议只要求 tool_call_id 对应）。"""
        function = call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        return ""

    async def _state_respond(self, ctx: TurnContext) -> str:
        """组装并返回出站消息。

        失败场景（ctx.error 非空）不走普通回复路径，而是生成系统通知
        （turn_internal_error）：模板提供固定的本地化致歉文案，ctx.error 作为
        纯技术诊断串填入 {detail} 段，并同步写入 metadata.error_detail 供日志/审计。
        """
        if ctx.error is not None:
            from nanobee.utils.notifications import build_notification

            # 透传真实错误详情（而非笼统的"内部错误"）。框架只透传、不编造错误内容。
            detail = ctx.error or ""
            ctx.outbound = build_notification(
                "turn_internal_error",
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                detail=detail,
            )
            ctx.outbound.metadata["error_detail"] = ctx.error
            # 错误轮次同样带耗时（错误路径可能绕过 SAVE，turn_latency_ms 尚未计算）
            if ctx.turn_latency_ms is None:
                ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
            ctx.outbound.metadata["latency_ms"] = ctx.turn_latency_ms
            return "ok"

        ctx.outbound = self._assemble_outbound(
            ctx.msg, ctx.final_content, self._turn_increment(ctx),
            ctx.exit_reason, ctx.had_injections,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    # --- 辅助方法 ---

    @staticmethod
    def _turn_increment(ctx: TurnContext) -> list[dict[str, Any]]:
        """本轮新增消息（锚点 ``initial_messages`` 之后的部分）。

        出站附件收集与轨迹落盘共用同一切片定义：只处理本轮产生的消息，历史消息
        **不参与**——历史里的 ``message`` 工具调用属于已完成的投递，重复扫描会把
        旧附件塞进之后每一轮的出站消息。历史也不做任何兼容处理（属数据噪音）。

        切片前提不满足（无锚点 / 消息被裁短）时返回空列表：宁可少收，不可重投。

        Args:
            ctx: 当前 turn 上下文

        Returns:
            本轮新增消息列表；前提不满足时为空列表
        """
        boundary = len(ctx.initial_messages)
        if boundary <= 0 or len(ctx.all_messages) < boundary:
            logger.warning(
                f"本轮增量切片前提不满足，跳过本轮增量处理（initial={boundary}, "
                f"all={len(ctx.all_messages)}, context={ctx.context_id}）"
            )
            return []
        return ctx.all_messages[boundary:]

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str | None,
        turn_messages: list[dict[str, Any]],
        exit_reason: str,
        had_injections: bool,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """从轮次结果组装出站消息。

        扫描 ``turn_messages``（**本轮新增消息**，由 :meth:`_turn_increment`
        产出）中的 ``message`` 工具调用，收集其声明的附件路径合并到出站消息中。
        正文的唯一来源是 ``final_content``——``message`` 工具只承载附件。
        """
        content = final_content or EMPTY_FINAL_RESPONSE_MESSAGE

        preview = content[:120] + "..." if len(content) > 120 else content
        logger.info("回复 {}: {}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)
        # 写入客观退出原因（completed / max_iterations），
        # 通道据此决策：max_iterations 时卡片内容可能不完整，需追加通知。
        meta["exit_reason"] = exit_reason

        # 收集本轮 message 工具调用中声明的附件路径（历史不参与，见 _turn_increment）
        from nanobee.agent.tools.message import collect_message_tool_media
        tool_media = collect_message_tool_media(turn_messages or [])
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
