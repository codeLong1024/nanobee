"""Nanobee Kernel - 极简内核"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop, OutboundMessage
from nanobee.kernel.context_manager import ContextManager
from nanobee.kernel.context_pipeline import ContextPipeline
from nanobee.kernel.event_bus import EventBus
from nanobee.kernel.lock_manager import LockManager
from nanobee.kernel.plugin_manager import PluginManager
from nanobee.kernel.router import ContextRouter, UnknownRouteError
from nanobee.kernel.sandbox import ContextSandbox, SandboxError
from nanobee.kernel.soul_guard import SoulGuard
from nanobee.kernel.tool_collector import ToolCollector
from nanobee.kernel.user_context import UserContext, UserMetadata
from nanobee.utils.observability import MetricsCollector, setup_structured_logging

logger = logging.getLogger(__name__)


from nanobee.agent.hook import AgentHook
from nanobee.kernel.skill_manager import SkillManager


class _StreamHook(AgentHook):
    """内部流式 Hook：桥接 Kernel.handle_message_streaming → AgentHook 系统。

    仅桥接 on_stream（流式增量），不桥接 on_stream_end。
    结束事件由调用者在 handle_message_streaming 返回后统一处理，
    避免运行器内部的 on_stream_end 与通道的二次发送产生时序冲突。
    """

    def __init__(self, on_stream: Any = None, on_stream_end: Any = None) -> None:
        super().__init__()
        self._on_stream = on_stream
        # on_stream_end 不使用：运行器内部触发会导致重复 _stream_end
        # 改为由 handle_message_streaming 返回后调用者统一发送
        _ = on_stream_end

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_stream(self, context: Any, delta: str) -> None:
        if self._on_stream and delta:
            try:
                await self._on_stream(delta)
            except Exception:
                logger.exception("[_StreamHook] on_stream callback failed, delta=%s...", delta[:80])

    async def on_stream_end(self, context: Any, *, resuming: bool = False) -> None:
        # no-op：结束事件由 handle_message_streaming 返回后统一处理
        pass

    def finalize_content(self, context: Any, content: str | None) -> str | None:
        """Pass-through: _StreamHook does not modify content."""
        return content


class NanobeeKernel:
    """Nanobee 内核

    内核只做三件事：
    1. 管理插件生命周期
    2. 路由消息到正确的上下文
    3. 保护灵魂文件（core.md）不被篡改
    """

    def __init__(
        self,
        config: dict | None = None,
        plugin_dirs: list[str] | None = None,
    ):
        """初始化内核

        Args:
            config: 配置字典（从 config.yaml 加载）
            plugin_dirs: 插件目录列表
        """
        self.config = config or {}
        self.work_dir = Path(self.config.get("work_dir", "~/.nanobee")).expanduser()

        # 核心组件
        self.event_bus = EventBus()
        self.metrics = MetricsCollector()
        resolved_plugin_dirs = plugin_dirs or self.config.get("plugin_dirs", ["builtin", "plugins"])
        self.plugin_manager = PluginManager(self, resolved_plugin_dirs)
        self.context_manager = ContextManager(self)
        self.skill_manager = SkillManager(self.work_dir / "skills")
        self.context_pipeline = ContextPipeline(self)
        self.soul_guard = SoulGuard(self)
        self.router = ContextRouter()

        # 从配置加载路由表
        routing_config = self.config.get("routing", {})
        if routing_config:
            self.router.load_from_config(routing_config)

        # Agent Loop（延迟初始化）
        self._agent_loop: AgentLoop | None = None

        self._booted = False

    async def boot(self) -> None:
        """启动内核

        按顺序执行：
        1. 加载配置
        2. 校验灵魂文件
        3. 扫描并加载插件
        4. 启动通道插件
        """
        if self._booted:
            logger.warning("内核已启动，跳过")
            return

        logger.info("正在启动 Nanobee 内核...")

        # 1. 校验灵魂文件
        await self.soul_guard.check()

        # 2. 扫描并加载插件
        self.plugin_manager.load_all()

        # 3. 启用所有插件
        for name in self.plugin_manager.list_plugins():
            self.plugin_manager.enable(name)

        # 3.1 注册工具插件到 AgentLoop（必须在插件加载完成后调用）
        if self._agent_loop:
            self._agent_loop._register_plugin_tools()

        # 先标记内核已启动，通道启动可能阻塞但不影响消息处理
        self._booted = True

        # 4. 启动通道插件（单个失败不阻塞整体启动）
        channels = self.plugin_manager.get_by_type("channel")
        for channel in channels:
            try:
                await channel.start()
            except Exception:
                logger.exception("通道插件 %s 启动失败，已跳过", getattr(channel, "name", "?"))

        logger.info("Nanobee 内核启动完成")

        # 发射启动事件
        await self.event_bus.publish("kernel.booted", {"kernel": self})

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

    async def handle_message(
        self,
        message: str,
        context_id: str = "default",
        sender_id: str = "user",
    ) -> OutboundMessage | None:
        """处理用户消息（阻塞式，无流式回调）。

        Args:
            message: 用户消息
            context_id: 上下文 ID
            sender_id: 发送者 ID，作为 context 目录标识

        Returns:
            Agent 回复（含可能的媒体附件路径）
        """
        return await self._handle_message_impl(message, context_id, sender_id=sender_id)

    async def handle_message_streaming(
        self,
        message: str,
        context_id: str = "default",
        *,
        on_stream: Any = None,
        on_stream_end: Any = None,
        sender_id: str = "user",
    ) -> OutboundMessage | None:
        """处理用户消息（支持流式回调）。

        流式文本块通过 on_stream(delta) 逐段回调，流结束通过
        on_stream_end(resuming=False) 通知。

        Args:
            message: 用户消息
            context_id: 上下文 ID
            on_stream: 每段文本增量回调，签名 async (delta: str) -> None
            on_stream_end: 流结束回调，签名 async (*, resuming: bool) -> None
            sender_id: 发送者 ID，作为 context 目录标识

        Returns:
            Agent 回复（含可能的媒体附件路径）
        """
        hook = _StreamHook(on_stream=on_stream, on_stream_end=on_stream_end)
        return await self._handle_message_impl(
            message, context_id, extra_hook=hook, sender_id=sender_id,
        )

    async def _handle_message_impl(
        self,
        message: str,
        context_id: str,
        *,
        extra_hook: Any = None,
        sender_id: str = "user",
    ) -> OutboundMessage | None:
        """处理用户消息的公共实现。

        Args:
            message: 用户消息
            context_id: 上下文 ID
            extra_hook: 可选的流式 Hook，桥接到 AgentLoop 的流式系统
            sender_id: 发送者 ID，作为 context 目录标识

        Returns:
            Agent 回复（OutboundMessage，含 .content 和 .media）
        """
        from nanobee.agent.loop import InboundMessage

        if not self._booted:
            raise RuntimeError("内核未启动，请先调用 boot()")

        if self._agent_loop is None:
            raise RuntimeError(
                "Agent Loop 未初始化。请先调用 boot_with_provider() "
                "或通过 set_agent_loop() 设置 Agent Loop。"
            )

        if self._agent_loop is not None:
            await self._agent_loop._connect_mcp()

        msg = InboundMessage(
            channel="direct",
            sender_id=sender_id,
            chat_id=context_id,
            content=message,
        )

        if extra_hook is not None:
            self._agent_loop._extra_hooks.append(extra_hook)

        # 不传入 context_id 参数,让 _process_message 使用 msg.context_id
        # 这样可以确保使用 sender_id 作为唯一标识(参考 nanobot 设计)
        try:
            response = await self._agent_loop._process_message(msg)
        finally:
            if extra_hook is not None and extra_hook in self._agent_loop._extra_hooks:
                self._agent_loop._extra_hooks.remove(extra_hook)

        return response



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

        Args:
            provider: LLM Provider 实例
            model: 模型名称（可选，使用 provider 默认值）
            **extra: 传递给 AgentLoop 的额外参数
        """
        actual_provider = provider

        # 从配置中提取 mcp_servers（如果未在 extra 中指定）
        if "mcp_servers" not in extra:
            extra["mcp_servers"] = self.config.get("mcp_servers", {})
        # 从配置中提取 memory_store_threshold（如果未在 extra 中指定）
        if "memory_store_threshold" not in extra:
            agents_config = self.config.get("agents", {})
            defaults = agents_config.get("defaults", {}) if isinstance(agents_config, dict) else {}
            memory_val = defaults.get("memory_store_threshold", 20)
            extra["memory_store_threshold"] = int(memory_val)

        self._agent_loop = AgentLoop.from_kernel(
            kernel=self,
            provider=actual_provider,
            workspace=self.work_dir,
            model=model,
            **extra,
        )

        await self.boot()

    @property
    def is_booted(self) -> bool:
        """内核是否已启动"""
        return self._booted

    @property
    def agent_loop(self) -> AgentLoop | None:
        """获取 Agent Loop 实例"""
        return self._agent_loop
