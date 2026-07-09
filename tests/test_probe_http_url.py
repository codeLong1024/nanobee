"""测试 ``_probe_http_url`` —— MCP HTTP server 连接前可达性探测。

该探测用于避免在端口关闭时进入 sse_client / streamable_http_client，
后者基于 anyio task group，清理时可能抛出 RuntimeError / ExceptionGroup
逃逸调用方的 try/except 并破坏事件循环。
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress

import pytest

from nanobee.agent.tools.mcp import _probe_http_url


@pytest.mark.asyncio
async def test_probe_reachable_local_server() -> None:
    """本地有监听的端口应探测成功返回 True。"""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _probe_http_url(f"http://127.0.0.1:{port}/") is True
    finally:
        server.close()
        with suppress(Exception):
            await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_unreachable_closed_port() -> None:
    """连接到已释放的本地端口应被拒，返回 False 且不抛异常。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # 端口立即释放 → 连接被拒绝
    assert await _probe_http_url(f"http://127.0.0.1:{port}/", timeout=0.3) is False


@pytest.mark.asyncio
async def test_probe_unreachable_non_routable() -> None:
    """不可路由地址 + 短超时 → 超时返回 False，且不破坏事件循环。"""
    assert await _probe_http_url("http://10.255.255.1:9/", timeout=0.3) is False


@pytest.mark.asyncio
async def test_probe_default_port_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式带端口时，http 解析为 80、https 解析为 443。"""
    captured: dict[str, object] = {}

    async def _fake_open_connection(host: str, port: int, **_kwargs: object) -> object:
        captured["host"] = host
        captured["port"] = port
        raise OSError("forced by test")

    monkeypatch.setattr(
        "nanobee.agent.tools.mcp.asyncio.open_connection", _fake_open_connection,
    )

    assert await _probe_http_url("https://example.test/x") is False
    assert captured["host"] == "example.test"
    assert captured["port"] == 443

    assert await _probe_http_url("http://example.test/x") is False
    assert captured["port"] == 80
