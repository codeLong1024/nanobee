"""内核集成测试"""

from __future__ import annotations

import asyncio

import pytest

from nanobee.agent.messages import OutboundMessage
from nanobee.kernel import NanobeeKernel
from nanobee.kernel.core_parser import CoreMDParser


@pytest.fixture
def temp_core_md(tmp_path):
    """创建临时 core.md 文件"""
    content = """# test core.md

## Soul
你是测试助手。

## Rules
- 保持简洁
"""
    core_md = tmp_path / "core.md"
    core_md.write_text(content, encoding="utf-8")
    return core_md


def test_core_md_parser(temp_core_md):
    """测试 core.md 解析器"""
    parser = CoreMDParser(temp_core_md)
    sections = parser.parse()

    assert "Soul" in sections
    assert "Rules" in sections
    assert "你是测试助手" in sections["Soul"]
    assert "保持简洁" in sections["Rules"]


def test_core_md_parser_hash(temp_core_md):
    """测试哈希计算"""
    parser = CoreMDParser(temp_core_md)
    hash1 = parser.compute_hash()
    hash2 = parser.compute_hash()

    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 = 64 hex chars


@pytest.mark.asyncio
async def test_kernel_boot(tmp_path):
    """测试内核启动"""
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
    }

    # 创建 core.md
    CoreMDParser.create_default(tmp_path / "core.md")

    kernel = NanobeeKernel(config=config)
    await kernel.boot()

    assert kernel.is_booted

    await kernel.shutdown()
    assert not kernel.is_booted


@pytest.mark.asyncio
async def test_shutdown_drains_mcp_connect_before_closing(tmp_path):
    """关停顺序：先等 MCP 连接任务落地，再关闭 MCP 连接。

    MCP 连接是「不阻塞启动」的后台任务，实例启动后立刻停止时会追上一个仍在飞行
    的连接。若先 ``close_mcp()``，此时 MCPManager 尚未登记任何 owner，close() 会
    直接返回；连接随后建成便再无调用方回收（连接 / 子进程 / task 三重泄漏）。
    """
    CoreMDParser.create_default(tmp_path / "core.md")
    kernel = NanobeeKernel(
        config={
            "data_dir": str(tmp_path),
            "core_md_path": str(tmp_path / "core.md"),
        },
    )
    await kernel.boot()

    order: list[str] = []

    class _FakeAgentLoop:
        """只记录关停调用顺序的替身（真实 AgentLoop 需要 LLM Provider）。"""

        def stop(self) -> None:
            order.append("stop")

        async def drain_hook_tasks(self, timeout_s: float) -> int:
            order.append("drain_hook_tasks")
            return 0

        async def close_mcp(self) -> None:
            order.append("close_mcp")

    kernel.set_agent_loop(_FakeAgentLoop())

    async def _connecting() -> None:
        await asyncio.sleep(0.05)
        order.append("mcp_landed")

    await kernel.channel_manager.start_channels([], connect_mcp=_connecting)
    await kernel.shutdown()

    assert "mcp_landed" in order, "关停必须等待 MCP 连接任务落地"
    assert order.index("mcp_landed") < order.index("close_mcp")


@pytest.mark.asyncio
async def test_command_interception_precedes_mcp_connect(tmp_path):
    """命令拦截必须**先于** MCP 连接：命令不得为挂死的 MCP server 付出等待。

    connect() 对卡死的 server 最长要等 CONNECT_ATTEMPT_TIMEOUT_S；命令是零
    token 路径（如 /stop、/help），把它们挡在连接等待之后会把最需要即时响应的
    操作拖死。
    """
    CoreMDParser.create_default(tmp_path / "core.md")
    kernel = NanobeeKernel(
        config={
            "data_dir": str(tmp_path),
            "core_md_path": str(tmp_path / "core.md"),
        },
    )
    await kernel.boot()

    calls = {"connect_mcp": 0}

    class _FakeAgentLoop:
        """替身：只记录 connect_mcp 是否被触达（真实 AgentLoop 需要 Provider）。"""

        def stop(self) -> None:
            pass

        async def close_mcp(self) -> None:
            pass

        async def connect_mcp(self) -> None:
            calls["connect_mcp"] += 1

    kernel.set_agent_loop(_FakeAgentLoop())

    class _FakeRouter:
        """总是接住消息的替身命令路由。"""

        async def dispatch(self, content, ctx):
            return OutboundMessage(channel="cli", chat_id="direct", content="/cmd ok", metadata={})

    kernel.command_router = _FakeRouter()  # type: ignore[assignment]

    response = await kernel.handle_message("/stop")

    assert response is not None and "/cmd ok" in response.content
    assert calls["connect_mcp"] == 0, "命令消息不得触发 MCP 连接"
