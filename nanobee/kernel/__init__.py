"""Nanobee Kernel - 极简内核"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop
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

logger = logging.getLogger(__name__)


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
        self.work_dir = Path(self.config.get("work_dir", ".")).expanduser()

        # 核心组件
        self.event_bus = EventBus()
        resolved_plugin_dirs = plugin_dirs or self.config.get("plugin_dirs", ["builtin", "plugins"])
        self.plugin_manager = PluginManager(self, resolved_plugin_dirs)
        self.context_manager = ContextManager(self)
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

        # 4. 启动通道插件
        channels = self.plugin_manager.get_by_type("channel")
        for channel in channels:
            await channel.start()

        self._booted = True
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

    async def handle_message(self, message: str, context_id: str = "default") -> str:
        """处理用户消息

        Args:
            message: 用户消息
            context_id: 上下文 ID

        Returns:
            Agent 回复
        """
        if not self._booted:
            raise RuntimeError("内核未启动，请先调用 boot()")

        # 需要先通过 set_agent_loop() 或 boot_with_provider() 初始化 Agent Loop
        if self._agent_loop is None:
            raise RuntimeError(
                "Agent Loop 未初始化。请先调用 boot_with_provider() "
                "或通过 set_agent_loop() 设置 Agent Loop。"
            )

        # 连接 MCP 服务器（在消息处理前确保 MCP 工具已注册）
        if self._agent_loop is not None:
            await self._agent_loop._connect_mcp()

        # 通过 AgentLoop._process_message 处理消息
        from nanobee.agent.loop import InboundMessage

        msg = InboundMessage(
            channel="direct",
            sender_id="user",
            chat_id=context_id,
            content=message,
        )
        response = await self._agent_loop._process_message(msg, context_id=context_id)
        return response.content if response else "No response generated."

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


__all__ = [
    "NanobeeKernel",
    "AgentLoop",
    "PluginManager",
    "ContextManager",
    "LockManager",
    "SoulGuard",
    "EventBus",
    "UserContext",
    "UserMetadata",
    "ContextRouter",
    "UnknownRouteError",
    "ContextSandbox",
    "SandboxError",
    "ToolCollector",
]
