"""channel_cli 插件交互循环测试 — 入站旁路收口后的直连内核路径。

覆盖：
1. 用户输入 → 直连 kernel.handle_message（content / context_id / channel / session_id / on_progress）
2. 响应经 send() 投递，context_id 为 ``<channel>:<chat_id>``
3. 空响应不投递
4. 内核未就绪时输出提示文案且不调用内核
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.builtin.channel_cli.plugin import ChannelCLIPlugin
from nanobee.outbound import OutboundMessage
from nanobee.plugins.base import PluginMetadata


def _make_plugin(response_content: str = "内核回复") -> tuple[ChannelCLIPlugin, MagicMock]:
    """构造已注入内核的 CLI 通道插件（不启动交互循环）。

    Args:
        response_content: mock 内核返回的回复正文。

    Returns:
        (插件实例, mock 内核)。
    """
    plugin = ChannelCLIPlugin(PluginMetadata(name="channel_cli", plugin_type="channel"))
    kernel = MagicMock()
    kernel.config = {}
    kernel.handle_message = AsyncMock(
        return_value=OutboundMessage(
            channel="channel_cli", chat_id="default", content=response_content,
        )
    )
    plugin.initialize(kernel)
    plugin.send = AsyncMock()
    plugin._running = True
    return plugin, kernel


@pytest.mark.asyncio
async def test_interaction_loop_calls_kernel_directly() -> None:
    """输入一轮后直连内核，并把回复交给 send() 投递。"""
    plugin, kernel = _make_plugin()

    with patch("builtins.input", side_effect=["  你好  ", EOFError]):
        await plugin._interaction_loop()

    args, kwargs = kernel.handle_message.call_args
    assert args == ("你好", "channel_cli:default")
    assert kwargs["channel"] == "channel_cli"
    assert kwargs["session_id"] == "cli:direct"
    assert callable(kwargs["on_progress"])

    assert plugin.send.await_count == 1
    sent, context_id = plugin.send.call_args.args
    assert sent.content == "内核回复"
    assert context_id == "channel_cli:default"
    assert plugin._running is False


@pytest.mark.asyncio
async def test_interaction_loop_skips_empty_response() -> None:
    """内核返回空内容时不投递（保持历史行为）。"""
    plugin, _kernel = _make_plugin(response_content="")

    with patch("builtins.input", side_effect=["你好", EOFError]):
        await plugin._interaction_loop()

    plugin.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_loop_notifies_when_kernel_missing() -> None:
    """内核未注入时输出提示文案，且不调用内核。"""
    plugin = ChannelCLIPlugin(PluginMetadata(name="channel_cli", plugin_type="channel"))
    plugin.send = AsyncMock()
    plugin._running = True

    with patch("builtins.input", side_effect=["你好", EOFError]):
        await plugin._interaction_loop()

    assert plugin.send.await_count == 1
    assert plugin.send.call_args.args[0].content == "[内核未就绪]"


# ============================================================
# 事件型出站的附件取舍（Phase 2 接线）
# ============================================================


def _make_bare_plugin() -> ChannelCLIPlugin:
    """未 mock send 的插件实例（走真实 send()，用于验证终端输出形态）。"""
    return ChannelCLIPlugin(PluginMetadata(name="channel_cli", plugin_type="channel"))


@pytest.mark.asyncio
async def test_event_with_attachment_prints_content_only(capsys) -> None:
    """事件型出站带附件：只打印正文，附件静默忽略（终端无附件投递能力）。"""
    plugin = _make_bare_plugin()

    await plugin._on_agent_outbound({
        "channel": "channel_cli",
        "chat_id": "default",
        "content": "周报已生成",
        "media": ["/data/周报.md"],
        "metadata": {},
    })

    out = capsys.readouterr().out
    assert "周报已生成" in out
    assert "周报.md" not in out


@pytest.mark.asyncio
async def test_event_pure_attachment_prints_nothing(capsys) -> None:
    """纯附件事件：CLI 无正文可打印——不崩溃、不报错、不产生输出。"""
    plugin = _make_bare_plugin()

    await plugin._on_agent_outbound({
        "channel": "channel_cli",
        "chat_id": "default",
        "content": "",
        "media": ["/data/周报.md"],
        "metadata": {},
    })

    assert capsys.readouterr().out == ""
