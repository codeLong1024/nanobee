"""Nanobee Kernel - 极简内核"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # OutboundMessage 和 AgentLoop 只在类型注解中使用，运行时通过方法内延迟导入
    from nanobee.agent.loop import AgentLoop, OutboundMessage

from nanobee.config.schema import Config
from nanobee.exceptions import ContextError
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.kernel.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.plugin_manager import PluginManager
from nanobee.kernel.router import ContextRouter, UnknownRouteError
from nanobee.kernel.runtime_events import KernelBooted, RuntimeEventBus
from nanobee.kernel.sandbox import ContextSandbox, SandboxError
from nanobee.kernel.soul_guard import SoulGuard
from nanobee.kernel.tool_collector import ToolCollector
from nanobee.kernel.user_context import UserContext, UserMetadata
from nanobee.utils.logger import logger

from nanobee.utils.observability import MetricsCollector, setup_structured_logging


from nanobee.kernel.skill_manager import SkillsLoader


class NanobeeKernel:
    """Nanobee 内核

    内核只做三件事：
    1. 管理插件生命周期
    2. 路由消息到正确的上下文
    3. 保护灵魂文件（core.md）不被篡改
    """

    def __init__(
        self,
        config: Config | dict | None = None,
        plugin_dirs: list[str] | None = None,
    ):
        """初始化内核

        Args:
            config: 配置对象（Config 实例或字典，从 nanobee.yaml 加载）
            plugin_dirs: 插件目录列表（覆盖配置中的 plugin_dirs）
        """
        # 统一为 Config 对象（允许传入 dict 保持向后兼容）
        if isinstance(config, dict):
            config = Config(**config)
        self.config = config or Config()
        self.work_dir = Path(self.config.work_dir).expanduser()

        # 核心组件
        self.event_bus = EventBus()              # 字符串 key 事件（供插件使用）
        self.runtime_events = RuntimeEventBus()  # 类型化运行时事件（内核内部通知）
        self.metrics = MetricsCollector()
        resolved_plugin_dirs = plugin_dirs or self.config.plugin_dirs or ["builtin", "plugins"]
        self.plugin_manager = PluginManager(self, resolved_plugin_dirs)
        self.context_manager = ContextManager(self)
        # 内置技能目录：nanobee/skills/
        _builtin_skills = Path(__file__).resolve().parent.parent / "skills"
        self.skill_manager = SkillsLoader(
            builtin_skills_dir=_builtin_skills,
        )
        self.soul_guard = SoulGuard(self)
        self.context_pipeline = ContextPipeline(
            core_md_path=self.config.core_md_path,
            skill_loader=self.skill_manager,
            soul_guard=self.soul_guard,
        )
        self.router = ContextRouter()

        # 从配置加载路由表
        if self.config.routing:
            self.router.load_from_config(self.config.routing)

        # Agent Loop（延迟初始化）
        self._agent_loop: AgentLoop | None = None

        self._booted = False

    async def boot(self) -> None:
        """启动内核核心组件

        按顺序执行：
        1. 加载配置
        2. 校验灵魂文件
        3. 扫描并加载插件
        4. 注册工具到 AgentLoop

        注意：不启动通道等后台服务，
        需调用 boot_services() 来启动。
        """
        if self._booted:
            logger.warning("内核已启动，跳过")
            return

        logger.info("正在启动 Nanobee 内核...")

        # 1. 校验灵魂文件
        await self.soul_guard.check()

        # 2. 扫描并加载插件
        self.plugin_manager.load_all()

        # 3. 启用插件（尊重 plugin.toml 中的 enabled 配置）
        for name in self.plugin_manager.list_plugins():
            descriptor = self.plugin_manager.get_descriptor(name)
            enabled_config = (descriptor.config or {}).get("enabled", True) if descriptor else True
            if enabled_config:
                self.plugin_manager.enable(name)
            else:
                logger.info("插件 {} 已配置为禁用状态，跳过启用", name)

        # 3.1 注册工具插件到 AgentLoop（必须在插件启用完成后调用，仅注册已启用的工具）
        if self._agent_loop:
            self._agent_loop._register_plugin_tools()

        self._booted = True

        logger.info("Nanobee 内核核心启动完成")

        # 发射启动事件（双总线：插件用字符串事件，内核用类型化事件）
        await self.event_bus.publish("kernel.booted", {"kernel": self})
        await self.runtime_events.publish(KernelBooted())

    async def boot_services(self) -> None:
        """启动后台服务（通道插件 + Heartbeat）

        仅在 Gateway 模式下调用，Agent CLI 模式不启动。
        """
        if getattr(self, "_services_started", False):
            logger.warning("后台服务已启动，跳过")
            return

        logger.info("正在启动 Nanobee 后台服务...")

        # 1. 启动通道插件（跳过非 Gateway 安全的通道，如 CLI）
        channels = self.plugin_manager.get_by_type("channel")
        for channel in channels:
            if not getattr(channel, "safe_for_gateway", True):
                logger.info("通道 {} 跳过 Gateway 启动（交互式通道）", getattr(channel, "name", "?"))
                continue
            try:
                await channel.start()
            except Exception:
                logger.exception("通道插件 {} 启动失败，已跳过", getattr(channel, "name", "?"))

        self._services_started = True
        logger.info("Nanobee 后台服务启动完成")

    async def handle_message(
        self,
        message: str,
        context_id: str = "default",
        *,
        media: list[str] | None = None,
        on_stream: Any = None,
        on_stream_end: Any = None,
        sender_id: str = "user",
    ) -> OutboundMessage | None:
        """处理用户消息。

        支持可选媒体附件和流式回调。
        流式文本块通过 on_stream(delta) 逐段回调，流结束通过
        on_stream_end(resuming=False) 通知。

        Args:
            message: 用户消息文本
            context_id: 上下文 ID
            media: 媒体附件路径列表（图片、文件等）
            on_stream: 每段文本增量回调，签名 async (delta: str) -> None
            on_stream_end: 流结束回调，签名 async (*, resuming: bool) -> None
            sender_id: 发送者 ID，作为 context 目录标识

        Returns:
            Agent 回复（含可能的媒体附件路径）
        """
        from nanobee.agent.hook import StreamBridgeHook
        hook = StreamBridgeHook(on_stream=on_stream, on_stream_end=on_stream_end) if on_stream else None
        return await self._handle_message_impl(
            message, context_id, media=media, extra_hook=hook, sender_id=sender_id,
        )

    async def _handle_message_impl(
        self,
        message: str,
        context_id: str,
        *,
        media: list[str] | None = None,
        extra_hook: Any = None,
        sender_id: str = "user",
    ) -> OutboundMessage | None:
        """处理用户消息的公共实现。

        使用与 _dispatch 相同的串行锁 + 待处理队列机制：
        - 同用户消息串行处理（LockManager）
        - 跨用户消息并行处理
        - 异常时返回友好错误消息而不是裸抛

        Args:
            message: 用户消息
            context_id: 上下文 ID
            media: 媒体附件路径列表
            extra_hook: 可选的流式 Hook，桥接到 AgentLoop 的流式系统
            sender_id: 发送者 ID，作为 context 目录标识

        Returns:
            Agent 回复（OutboundMessage，含 .content 和 .media）
        """
        from nanobee.agent.loop import InboundMessage, OutboundMessage

        if not self._booted:
            raise ContextError("内核未启动，请先调用 boot()")

        if self._agent_loop is None:
            raise ContextError(
                "Agent Loop 未初始化。请先调用 boot_with_provider() "
                "或通过 set_agent_loop() 设置 Agent Loop。"
            )

        # 连接 MCP 服务器（此时 self._agent_loop 必然非 None）
        await self._agent_loop._connect_mcp()

        msg = InboundMessage(
            channel="direct",
            sender_id=sender_id,
            chat_id=context_id,
            content=message,
            media=media or [],
        )

        if extra_hook is not None:
            self._agent_loop._extra_hooks.append(extra_hook)

        agent = self._agent_loop
        try:
            # 使用串行锁 + 待处理队列，同 _dispatch 设计
            key = msg.context_id
            pending = asyncio.Queue(maxsize=20)
            agent._pending_queues[key] = pending

            try:
                async with agent._lock_manager.acquire(key):
                    try:
                        response = await agent._process_message(
                            msg, pending_queue=pending,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("处理上下文 {} 的消息出错", key)
                        response = OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="抱歉，处理消息时发生内部错误。",
                        )
            finally:
                # 排空待处理队列（同 _dispatch）
                queue = agent._pending_queues.pop(key, None)
                if queue is not None:
                    leftover = 0
                    while True:
                        try:
                            queue.get_nowait()
                            leftover += 1
                        except asyncio.QueueEmpty:
                            break
                    if leftover:
                        logger.info("上下文 {} 有 {} 条剩余消息被丢弃", key, leftover)

            return response
        finally:
            if extra_hook is not None and extra_hook in agent._extra_hooks:
                agent._extra_hooks.remove(extra_hook)



    def set_agent_loop(self, agent_loop: AgentLoop) -> None:
        """设置 Agent Loop 实例。

        Args:
            agent_loop: AgentLoop 实例
        """
        self._agent_loop = agent_loop

    async def boot_with_provider(
        self,
        provider: Any,
        model: str | None = None,
        **extra: Any,
    ) -> None:
        """使用指定的 LLM Provider 启动内核并初始化 Agent Loop。

        注意：仅启动核心组件（灵魂校验 + 插件加载 + 工具注册），
        不启动通道等后台服务。
        如需完整服务栈，请额外调用 boot_services()。

        Args:
            provider: LLM Provider 实例
            model: 模型名称（可选，使用 provider 默认值）
            **extra: 传递给 AgentLoop 的额外参数
        """
        # 延迟导入避免循环依赖（AgentLoop → kernel → AgentLoop）
        from nanobee.agent.loop import AgentLoop

        actual_provider = provider
        self._llm_provider = actual_provider
        self._llm_model = model or getattr(actual_provider, "model", None)

        self._agent_loop = AgentLoop.from_kernel(
            provider=actual_provider,
            workspace=self.work_dir,
            context_manager=self.context_manager,
            context_pipeline=self.context_pipeline,
            event_bus=self.event_bus,
            plugin_manager=self.plugin_manager,
            skill_manager=self.skill_manager,
            router=self.router,
            config=self.config,
            model=model,
            **extra,
        )

        await self.boot()

    @classmethod
    async def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        model: str | None = None,
        plugin_dirs: list[str] | None = None,
        start_services: bool = False,
    ) -> NanobeeKernel:
        """从配置文件创建并启动内核。

        一行完成：配置加载 → Provider 创建 → Boot 全流程。
        默认不启动后台服务（通道），
        设置 start_services=True 可开启 Gateway 模式。

        Args:
            config_path: 配置文件路径。为 None 时使用默认配置。
            model: 模型名称（可选，覆盖配置中的 model）
            plugin_dirs: 插件目录列表（可选，覆盖配置中的 plugin_dirs）
            start_services: 是否启动后台服务（通道）

        Returns:
            已启动的 NanobeeKernel 实例
        """
        from nanobee.config.loader import load_config, resolve_config_env_vars
        from nanobee.providers.factory import make_provider

        if config_path is not None:
            cfg = resolve_config_env_vars(load_config(Path(config_path)))
        else:
            cfg = Config()

        actual_model = model or cfg.agents.defaults.model

        kernel = cls(config=cfg, plugin_dirs=plugin_dirs)
        provider = make_provider(cfg, model=actual_model)
        await kernel.boot_with_provider(provider, model=actual_model)

        if start_services:
            await kernel.boot_services()

        return kernel

    @property
    def is_booted(self) -> bool:
        """内核是否已启动"""
        return self._booted

    @property
    def agent_loop(self) -> AgentLoop | None:
        """获取 Agent Loop 实例"""
        return self._agent_loop

    async def shutdown(self) -> None:
        """关闭内核"""
        logger.info("正在关闭 Nanobee 内核...")

        # 停止 Agent Loop
        if self._agent_loop is not None:
            self._agent_loop.stop()
            await self._agent_loop.close_mcp()

        # 停止所有通道
        channels = self.plugin_manager.get_by_type("channel")
        for channel in channels:
            await channel.stop()

        # 关闭 MCP 连接（AgentLoop 内置能力）
        if self._agent_loop is not None:
            await self._agent_loop.close_mcp()

        # 卸载所有插件
        self.plugin_manager.unload_all()

        self._booted = False
        logger.info("Nanobee 内核已关闭")
