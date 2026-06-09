"""测试 MCPManager — MCP 连接管理器。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nanobee.agent.mcp_manager import MCPManager
from nanobee.agent.tools.registry import ToolRegistry


class TestMCPManagerInit:
    """MCPManager 初始化测试。"""

    def test_default_init(self):
        """默认初始化，无 MCP 服务器。"""
        mgr = MCPManager()
        assert not mgr.connected
        assert not mgr.has_servers
        assert mgr.stacks == {}

    def test_with_servers(self):
        """带服务器配置初始化。"""
        mgr = MCPManager({"my-server": {"type": "stdio", "command": "echo"}})
        assert not mgr.connected
        assert mgr.has_servers
        assert mgr.stacks == {}


class TestMCPManagerConnect:
    """MCPManager.connect() 测试。"""

    @pytest.mark.asyncio
    async def test_no_servers_noop(self):
        """无服务器配置时 connect 是空操作。"""
        mgr = MCPManager()
        tools = ToolRegistry()
        await mgr.connect(tools)
        assert not mgr.connected
        assert not mgr._connecting

    @pytest.mark.asyncio
    async def test_already_connected_noop(self):
        """已连接时 connect 是空操作。"""
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})
        mgr._connected = True
        tools = ToolRegistry()
        with patch("nanobee.agent.tools.mcp.connect_mcp_servers") as mock_connect:
            await mgr.connect(tools)
            mock_connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_connecting_noop(self):
        """正在连接时 connect 是空操作。"""
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})
        mgr._connecting = True
        tools = ToolRegistry()
        with patch("nanobee.agent.tools.mcp.connect_mcp_servers") as mock_connect:
            await mgr.connect(tools)
            mock_connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """成功连接 MCP 服务器。"""
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})
        tools = ToolRegistry()
        mock_stacks = {"s1": AsyncMock()}
        with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=AsyncMock(return_value=mock_stacks)) as mock_connect:
            await mgr.connect(tools)
            mock_connect.assert_awaited_once_with({"s1": {"type": "stdio", "command": "echo"}}, tools)
            assert mgr.connected
            assert mgr.stacks == mock_stacks

    @pytest.mark.asyncio
    async def test_connect_failure_clears_stacks(self):
        """连接失败时清除所有栈。"""
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})
        tools = ToolRegistry()
        with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=AsyncMock(side_effect=RuntimeError("connection failed"))):
            await mgr.connect(tools)
            assert not mgr.connected
            assert not mgr._connecting
            assert mgr.stacks == {}

    @pytest.mark.asyncio
    async def test_connect_cancelled_clears_stacks(self):
        """连接被取消时静默处理并清除所有栈。"""
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})
        tools = ToolRegistry()
        with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=AsyncMock(side_effect=asyncio.CancelledError)):
            await mgr.connect(tools)
        # CancelledError 被静默吞掉（与原始 AgentLoop 行为一致）
        assert mgr.stacks == {}
        assert not mgr._connecting
        assert not mgr.connected


class TestMCPManagerClose:
    """MCPManager.close() 测试。"""

    @pytest.mark.asyncio
    async def test_close_clears_state(self):
        """关闭后状态重置。"""
        mgr = MCPManager()
        mgr._connected = True
        await mgr.close()
        assert not mgr.connected
        assert not mgr._connecting
        assert mgr.stacks == {}

    @pytest.mark.asyncio
    async def test_close_closes_stacks(self):
        """关闭时调用每个栈的 aclose。"""
        mgr = MCPManager()
        mock_stack = AsyncMock()
        mgr._stacks = {"s1": mock_stack}
        mgr._connected = True
        await mgr.close()
        mock_stack.aclose.assert_awaited_once()
        assert mgr.stacks == {}


class TestMCPManagerProperties:
    """MCPManager 属性测试。"""

    def test_has_servers_empty(self):
        """空配置时 has_servers 为 False。"""
        assert not MCPManager().has_servers
        assert not MCPManager({}).has_servers

    def test_has_servers_nonempty(self):
        """有配置时 has_servers 为 True。"""
        assert MCPManager({"s1": {}}).has_servers
