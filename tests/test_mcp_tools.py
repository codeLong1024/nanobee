"""MCP 工具注册 / 注销的所有权语义测试（tools/mcp.py）。

覆盖两条结构性约束：
1. **注销与重连回调按 wrapper 归属（server_name）匹配**，不能按工具名前缀——
   server 名互为前缀（``a`` 与 ``a_b``）时前缀匹配会误伤对方。
2. **connect_mcp_servers 一次只允许一个 server**——同一 task 持有多个 server
   的 cancel scope 只能整体逆序退出，重连语义不成立（由 MCPManager 编排）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nanobee.agent.tools.mcp import (
    MCPToolWrapper,
    attach_reconnect_handlers,
    connect_mcp_servers,
    unregister_server_tools,
)
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger


def _tool(server_name: str, name: str) -> MCPToolWrapper:
    """构造一个归属明确的 MCP 工具 wrapper（session 不参与本组用例）。"""
    tool_def = type(
        "_Def",
        (),
        {
            "name": name,
            "description": name,
            "inputSchema": {"type": "object", "properties": {}},
        },
    )()
    return MCPToolWrapper(object(), server_name, tool_def)


@pytest.mark.asyncio
async def test_unregister_matches_ownership_not_prefix() -> None:
    """注销 server ``a`` 不得连带删掉 server ``a_b`` 的工具。

    名称前缀 ``mcp_a_`` 同时是 ``mcp_a_b_x`` 的前缀：按前缀匹配会让 a_b 的工具
    被误删，且此后无法自恢复（重连回调也挂到了错误归属上）。
    """
    registry = ToolRegistry()
    registry.register(_tool("a", "ping"))
    registry.register(_tool("a_b", "ping"))

    removed = unregister_server_tools(registry, "a")

    assert removed == 1, "只应注销属于 server 'a' 的那一个工具"
    assert registry.get("mcp_a_ping") is None
    assert registry.get("mcp_a_b_ping") is not None, "server 'a_b' 的工具不得被误删"


@pytest.mark.asyncio
async def test_attach_reconnect_handlers_matches_ownership() -> None:
    """重连回调只挂到指定 server 的工具上（同样按归属而非前缀）。"""
    registry = ToolRegistry()
    target = _tool("a", "ping")
    other = _tool("a_b", "ping")
    registry.register(target)
    registry.register(other)

    async def _reconnect(server_name: str, tool_name: str, stale_tool: object) -> object | None:
        return None

    attach_reconnect_handlers(registry, ["a"], _reconnect)

    assert target._reconnect is _reconnect
    assert other._reconnect is None, "server 'a_b' 的工具不得被挂上回调"


@pytest.mark.asyncio
async def test_connect_mcp_servers_rejects_multiple_servers() -> None:
    """一次传入多个 server 必须被拒绝（结构性约束的运行时断言）。"""
    with pytest.raises(ValueError, match="一次只允许连接一个"):
        await connect_mcp_servers({"s1": {}, "s2": {}}, ToolRegistry())


@pytest.mark.asyncio
async def test_connect_mcp_servers_rejects_unconfigured_server() -> None:
    """既无 command 也无 url 的 server 直接跳过（不建 stack、不抛异常）。"""
    result = await connect_mcp_servers({"s1": {}}, ToolRegistry())

    assert result == {}


@pytest.mark.asyncio
async def test_connect_failure_log_does_not_leak_url_key() -> None:
    """连接失败日志不得泄漏 URL query 里的 key（脱敏责任归连接层）。

    第三方（httpx/anyio）的异常消息内嵌完整请求 URL，query 携带网关 key，而
    ``logger.exception`` 会把 traceback 连同 ``str(exc)`` 一并写进日志。往下的
    第三方文案不可控，本层是第一个 nanobee 自有边界，必须在此收口。
    """
    url = "https://mcp-gw.example.com/server/abc?key=SUPERSECRET"
    messages: list[str] = []

    async def _boom(_url: str, timeout: float = 3.0) -> bool:
        # 复刻 httpx 的异常文案形态：消息内嵌完整 URL（含 query key）
        raise RuntimeError(f"Client error '403 Forbidden' for url '{url}'")

    sink_id = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        with patch("nanobee.agent.tools.mcp._probe_http_url", new=_boom):
            result = await connect_mcp_servers(
                {"s1": {"type": "sse", "url": url}}, ToolRegistry(),
            )
    finally:
        logger.remove(sink_id)

    assert result == {}
    assert any("failed to connect" in message for message in messages), "连接失败必须留痕"
    assert not any("SUPERSECRET" in message for message in messages), "日志不得泄漏 URL 中的 key"
    assert any("mcp-gw.example.com/server/abc" in message for message in messages), (
        "脱敏后仍应保留可定位的 URL 主干"
    )
