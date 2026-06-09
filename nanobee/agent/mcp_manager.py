"""MCP 连接管理器 — 管理 MCP 服务器连接生命周期。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger


class MCPManager:
    """管理 MCP 服务器连接生命周期。

    职责：
    - 懒加载连接配置的 MCP 服务器
    - 管理连接状态（已连接/连接中）
    - 关闭所有 MCP 连接
    """

    def __init__(self, mcp_servers: dict | None = None) -> None:
        """初始化 MCP 管理器。

        Args:
            mcp_servers: MCP 服务器配置字典，key 为服务器名，value 为配置
        """
        self._servers: dict = mcp_servers or {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._connected: bool = False
        self._connecting: bool = False

    @property
    def connected(self) -> bool:
        """是否已连接至少一个 MCP 服务器。"""
        return self._connected

    @property
    def stacks(self) -> dict[str, AsyncExitStack]:
        """获取所有 MCP 连接的栈。"""
        return dict(self._stacks)

    @property
    def has_servers(self) -> bool:
        """是否有配置的 MCP 服务器。"""
        return bool(self._servers)

    async def connect(self, tools: ToolRegistry) -> None:
        """懒加载连接配置的 MCP 服务器。

        幂等：已连接或连接中时跳过。
        失败时记录日志，不抛出异常。

        Args:
            tools: 工具注册表，MCP 工具将注册到此注册表
        """
        if self._connected or self._connecting or not self._servers:
            return

        self._connecting = True
        from nanobee.agent.tools.mcp import connect_mcp_servers

        try:
            logger.info("MCP: 开始连接 {count} 个服务器", count=len(self._servers))
            self._stacks = await connect_mcp_servers(self._servers, tools)
            if self._stacks:
                self._connected = True
                logger.info("MCP: 成功连接 {count} 个服务器", count=len(self._stacks))
            else:
                logger.warning("MCP: 没有 MCP 服务器成功连接（下次消息时重试）")
        except asyncio.CancelledError:
            logger.warning("MCP 连接被取消（下次消息时重试）")
            self._stacks.clear()
        except BaseException as e:
            logger.warning("MCP 服务器连接失败（下次消息时重试）: {error}", error=e)
            self._stacks.clear()
        finally:
            self._connecting = False

    async def close(self) -> None:
        """关闭所有 MCP 连接。

        幂等：多次调用安全。
        """
        for name, stack in self._stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP 服务器 '{name}' 清理错误（可忽略）", name=name)
        self._stacks.clear()
        self._connected = False
        self._connecting = False

    def close_nowait(self) -> None:
        """非阻塞关闭：在事件循环中创建 task 关闭所有 MCP 连接。"""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.close())
