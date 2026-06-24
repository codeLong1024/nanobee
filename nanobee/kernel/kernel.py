"""Nanobee Kernel - 极简内核"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # OutboundMessage 和 AgentLoop 只在类型注解中使用，运行时通过方法内延迟导入
    from nanobee.agent.loop import AgentLoop
    from nanobee.agent.messages import OutboundMessage

from nanobee.config.schema import Config
from nanobee.exceptions import ContextError
from nanobee.kernel.command_router import CommandContext, CommandRouter
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.events.event_bus import EventBus
from nanobee.kernel.plugin_manager import PluginManager
from nanobee.kernel.router import ContextRouter, UnknownRouteError
from nanobee.events.runtime_events import KernelBooted, RuntimeEventBus
from nanobee.session.session_manager import SessionManager
from nanobee.kernel.soul_guard import SoulGuard
from nanobee.kernel.user_context import UserContext, UserMetadata
from nanobee.utils.logger import logger

from nanobee.utils.notifications import build_notification
from nanobee.utils.observability import MetricsCollector


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
        self.data_dir = Path(self.config.data_dir).expanduser()

        # 核心组件
        self.event_bus = EventBus()              # 字符串 key 事件（供插件使用）
        self.runtime_events = RuntimeEventBus()  # 类型化运行时事件（内核内部通知）
        self.metrics = MetricsCollector()
        # 默认插件目录：相对于 nanobee 包位置（兼容 pip install 和 tar 部署）
        _package_builtin = str(Path(__file__).resolve().parent.parent / "builtin")
        resolved_plugin_dirs = plugin_dirs or self.config.plugin_dirs or [_package_builtin]
        self.plugin_manager = PluginManager(self, resolved_plugin_dirs)
        self.context_manager = ContextManager(self)
        # Session 管理器（管理多会话，存储于 <data_dir>/users/<user_id>/sessions/）
        self.session_manager = SessionManager(self.data_dir / "users")
        # 内置技能目录：nanobee/skills/
        _builtin_skills = Path(__file__).resolve().parent.parent / "skills"
        # 实例级技能目录：<data_dir>/skills/（管理员配属，实例内所有用户共享）
        _instance_skills = self.data_dir / "skills"
        self.skill_manager = SkillsLoader(
            builtin_skills_dir=_builtin_skills,
            instance_skills_dir=_instance_skills,
            enabled_instance_skills=self.config.skills.enabled,
        )
        self._core_md_path = Path(self.config.core_md_path).expanduser()
        self.soul_guard = SoulGuard(self, core_md_path=str(self._core_md_path))
        self.context_pipeline = ContextPipeline(
            core_md_path=str(self._core_md_path),
            skill_loader=self.skill_manager,
            soul_guard=self.soul_guard,
        )
        self.router = ContextRouter()

        # 命令路由系统（Slash Command）
        self.command_router = CommandRouter()
        # 活跃 turn 追踪：context_id → asyncio.Task，用于 /stop 取消
        self._active_turns: dict[str, asyncio.Task] = {}

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

        # 3. 启用插件
        # 优先级：config.yaml plugins.<name>.enabled > plugin.toml [config].enabled > 默认 True
        for name in self.plugin_manager.list_plugins():
            descriptor = self.plugin_manager.get_descriptor(name)
            # 从 config.yaml 的 plugins.<name>.enabled 读取（优先）
            cfg_plugins = self.config.plugins or {}
            cfg_enabled = cfg_plugins.get(name, {}).get("enabled") if isinstance(cfg_plugins, dict) else None
            # 从 plugin.toml 的 [config].enabled 读取（备选）
            toml_enabled = (descriptor.config or {}).get("enabled", True) if descriptor else True
            enabled = cfg_enabled if cfg_enabled is not None else toml_enabled
            if enabled:
                self.plugin_manager.enable(name)
            else:
                logger.info("插件 {} 已配置为禁用状态，跳过启用", name)

        # 3.1 注册工具插件到 AgentLoop（必须在插件启用完成后调用，仅注册已启用的工具）
        if self._agent_loop:
            self._agent_loop.register_plugin_tools()

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

        # 1. 启动通道插件（后台任务，不与健康服务器串行阻塞）
        channels = self.plugin_manager.get_by_type("channel")
        self._channel_tasks: list[asyncio.Task] = []

        def _log_channel_error(name: str) -> Any:
            """返回一个 done callback，捕获通道任务异常并记录日志。"""
            def _cb(t: asyncio.Task) -> None:
                try:
                    t.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("通道 {} 后台任务异常退出", name)
            return _cb

        for channel in channels:
            if not getattr(channel, "safe_for_gateway", True):
                logger.info("通道 {} 跳过 Gateway 启动（交互式通道）", getattr(channel, "name", "?"))
                continue
            chan_name = getattr(channel, "name", "?")
            try:
                task = asyncio.create_task(channel.start())
                task.add_done_callback(_log_channel_error(chan_name))
                self._channel_tasks.append(task)
            except Exception:
                logger.exception("通道插件 {} 启动失败，已跳过", chan_name)

        # 2. 主动连接 MCP 服务器（后台任务，不阻塞启动）
        if self._agent_loop is not None:
            asyncio.ensure_future(self._agent_loop._connect_mcp())

        self._services_started = True
        logger.info("Nanobee 后台服务启动完成")

    async def handle_message(
        self,
        message: str,
        context_id: str = "default",
        *,
        channel: str = "direct",
        media: list[str] | None = None,
        on_stream: Any = None,
        on_stream_end: Any = None,
        on_progress: Any = None,
        sender_id: str = "user",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """处理用户消息。

        支持可选媒体附件和流式回调。
        流式文本块通过 on_stream(delta) 逐段回调，流结束通过
        on_stream_end(resuming=False) 通知。
        on_progress(delta, *, tool_hint, tool_events) 用于工具执行进度通知。

        Args:
            message: 用户消息文本
            context_id: 上下文 ID（通常为用户 ID）
            channel: 来源通道名（如 channel_dingtalk），默认 "direct"
            media: 媒体附件路径列表（图片、文件等）
            on_stream: 每段文本增量回调，签名 async (delta: str) -> None
            on_stream_end: 流结束回调，签名 async (*, resuming: bool) -> None
            on_progress: 进度回调，签名 async (delta, *, tool_hint, tool_events) -> None
            sender_id: 发送者 ID，作为 context 目录标识
            session_id: 会话 ID（格式 channel:chat_id，None 时自动派生）
            metadata: 通道特定的元数据（如 sender_staff_id、sender_name 等）

        Returns:
            Agent 回复（含可能的媒体附件路径）
        """
        from nanobee.agent.hook import StreamBridgeHook
        hook = StreamBridgeHook(on_stream=on_stream, on_stream_end=on_stream_end) if on_stream else None
        return await self._handle_message_impl(
            message, context_id, channel=channel, media=media,
            extra_hook=hook, on_progress=on_progress,
            sender_id=sender_id, session_id=session_id,
            metadata=metadata,
        )

    async def _handle_message_impl(
        self,
        message: str,
        context_id: str,
        *,
        channel: str = "direct",
        media: list[str] | None = None,
        extra_hook: Any = None,
        on_progress: Any = None,
        sender_id: str = "user",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage | None:
        """处理用户消息的公共实现。

        使用 AgentLoop.dispatch 内部的串行保证：
        - 同用户消息串行处理（由 AgentLoop.dispatch 内部保证）
        - 跨用户消息并行处理
        - 异常时返回友好错误消息而不是裸抛

        Args:
            message: 用户消息
            context_id: 上下文 ID
            channel: 来源通道名（如 channel_dingtalk），默认 "direct"
            media: 媒体附件路径列表
            extra_hook: 可选的流式 Hook，桥接到 AgentLoop 的流式系统
            sender_id: 发送者 ID，作为 context 目录标识
            metadata: 通道特定的元数据（如 sender_staff_id、sender_name 等）

        Returns:
            Agent 回复（OutboundMessage，含 .content 和 .media）
        """
        from nanobee.agent.messages import InboundMessage, OutboundMessage

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
            channel=channel,
            sender_id=sender_id,
            chat_id=context_id,
            content=message,
            media=media or [],
            session_id_override=session_id,
            metadata=metadata or {},
        )

        # ── 命令拦截（锁之前，零 token 消耗） ──
        if getattr(self, "command_router", None) is not None:
            cmd_ctx = CommandContext(msg=msg, kernel=self)
            cmd_response = await self.command_router.dispatch(msg.content, cmd_ctx)
            if cmd_response is not None:
                return cmd_response

        agent = self._agent_loop
        key = msg.context_id

        # 追踪当前 Task，供 /stop 命令取消
        task = asyncio.current_task()
        if task is not None:
            self._active_turns[key] = task

        try:
            response = await agent.dispatch(
                msg,
                extra_hook=extra_hook,
                on_progress=on_progress,
            )
        except asyncio.CancelledError:
            logger.info("上下文 {} 的 turn 已被取消", key)
            response = build_notification(
                "turn_cancelled",
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
        except Exception:
            logger.exception("处理上下文 {} 的消息出错", key)
            response = build_notification(
                "turn_internal_error",
                channel=msg.channel,
                chat_id=msg.chat_id,
            )
        finally:
            self._active_turns.pop(key, None)

        return response

    # ── 统一消息注入入口（替代 dispatcher.enqueue_message） ───────────

    def inject_message(self, msg: "InboundMessage") -> None:
        """注入消息到处理管道（供子代理结果等异步通知使用）。

        统一入口：对齐 nanobot bus.publish_inbound 模式。
        - 当前上下文有活跃 turn 时，中轮注入（非阻塞）
        - 无活跃 turn 时，创建后台任务触发新 turn

        Args:
            msg: 待注入的入站消息（含 _subagent_auto_trigger 等元数据标记）
        """
        agent = self._agent_loop
        if agent is None:
            logger.warning("inject_message: AgentLoop 未初始化，丢弃消息")
            return

        if agent.try_inject(msg):
            return

        # 新轮触发：创建后台任务，不阻塞调用者
        asyncio.create_task(self._handle_injected_message(msg))
        logger.debug("已创建新 turn 后台任务处理注入消息，上下文 {}", msg.context_id)

    async def _handle_injected_message(self, msg: "InboundMessage") -> None:
        """处理注入消息：调用 handle_message 并通过 EventBus 发布结果。

        后台任务入口，不阻塞子代理 _announce_result 回调。
        """
        try:
            response = await self.handle_message(
                message=msg.content,
                context_id=msg.context_id,
                channel=msg.channel,
                media=msg.media,
                sender_id=msg.sender_id,
                session_id=msg.session_id_override,
                metadata=msg.metadata,
            )
            if response is not None and response.content:
                await self.event_bus.publish("agent.outbound", {
                    "channel": response.channel,
                    "chat_id": response.chat_id,
                    "content": response.content,
                    "metadata": response.metadata,
                })
        except Exception:
            logger.exception("处理注入消息时出错，上下文 {}", msg.context_id)

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

        已废弃：请使用 ``nanobee.bootstrap.bootstrap()`` 组合根。

        注意：仅启动核心组件（灵魂校验 + 插件加载 + 工具注册），
        不启动通道等后台服务。
        如需完整服务栈，请额外调用 boot_services()。

        Args:
            provider: LLM Provider 实例
            model: 模型名称（可选，使用 provider 默认值）
            **extra: 传递给 AgentLoop 的额外参数
        """
        logger.warning(
            "boot_with_provider() 已废弃，请使用 nanobee.bootstrap.bootstrap() 组合根",
        )
        import warnings
        warnings.warn(
            "boot_with_provider() is deprecated, use nanobee.bootstrap.bootstrap() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        # 延迟导入避免循环依赖（AgentLoop → kernel → AgentLoop）
        from nanobee.agent.loop import AgentLoop

        actual_provider = provider
        self._llm_provider = actual_provider
        self._llm_model = model or getattr(actual_provider, "model", None)

        self._agent_loop = AgentLoop.from_kernel(
            provider=actual_provider,
            workspace=self.data_dir,
            context_manager=self.context_manager,
            context_pipeline=self.context_pipeline,
            session_manager=self.session_manager,
            event_bus=self.event_bus,
            plugin_manager=self.plugin_manager,
            skill_manager=self.skill_manager,
            router=self.router,
            config=self.config,
            model=model,
            message_injector=self.inject_message,
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

        已废弃：请使用 ``nanobee.bootstrap.bootstrap()`` 组合根。

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
        logger.warning(
            "from_config() 已废弃，请使用 nanobee.bootstrap.bootstrap() 组合根",
        )
        import warnings
        warnings.warn(
            "from_config() is deprecated, use nanobee.bootstrap.bootstrap() instead",
            DeprecationWarning,
            stacklevel=2,
        )
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

        # 等待后台通道任务退出（取消后等待 3s 超时兜底）
        for task in getattr(self, "_channel_tasks", []):
            if not task.done():
                task.cancel()
        if getattr(self, "_channel_tasks", []):
            await asyncio.wait(
                self._channel_tasks, timeout=3,
                return_when=asyncio.ALL_COMPLETED,
            )

        # 卸载所有插件
        self.plugin_manager.unload_all()

        self._booted = False
        logger.info("Nanobee 内核已关闭")
