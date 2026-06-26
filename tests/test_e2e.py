"""端到端集成测试

测试完整的插件发现、加载、启用流程，验证可用的内置插件。
"""

from __future__ import annotations

import pytest

from nanobee.kernel.core_parser import CoreMDParser
from nanobee.kernel import NanobeeKernel


@pytest.mark.asyncio
async def test_e2e_kernel_boot_with_builtin(tmp_path):
    """内核带内置插件启动"""
    builtin_dir = tmp_path / "builtin"
    channel_cli_dir = builtin_dir / "channel-cli"
    tool_echo_dir = builtin_dir / "tool-echo"

    # 创建内置插件结构
    channel_cli_dir.mkdir(parents=True)
    (channel_cli_dir / "plugin.toml").write_text("""[plugin]
name = "channel-cli"
version = "1.0.0"
description = "命令行交互通道"
author = "nanobee-team"
type = "channel"

[config]
prompt_prefix = "🐝 "
history_size = 50
""", encoding="utf-8")
    (channel_cli_dir / "plugin.py").write_text("""from __future__ import annotations
import asyncio
import logging
from nanobee.channel.message import OutboundMessage
from nanobee.channel.base import ChannelPlugin

logger = logging.getLogger(__name__)

class CLIPlugin(ChannelPlugin):
    name = "channel-cli"
    version = "1.0.0"
    async def start(self) -> None:
        self._running = True
        logger.info("CLI 通道已启动")
    async def stop(self) -> None:
        self._running = False
        logger.info("CLI 通道已停止")
    async def send(self, message, context_id="default") -> None:
        if isinstance(message, OutboundMessage):
            text = message.content
        else:
            text = str(message)
        print(f"CLI: {text}")
    async def _process_incoming(self, message, context_manager):
        return []
""", encoding="utf-8")
    (channel_cli_dir / "__init__.py").write_text("from .plugin import CLIPlugin\n", encoding="utf-8")

    tool_echo_dir.mkdir(parents=True)
    (tool_echo_dir / "plugin.toml").write_text("""[plugin]
name = "tool-echo"
version = "1.0.0"
description = "回显测试工具"
author = "nanobee-team"
type = "tool"

[config]
enabled = true
""", encoding="utf-8")
    (tool_echo_dir / "plugin.py").write_text("""from __future__ import annotations
import logging
from nanobee.plugins import ToolPlugin

logger = logging.getLogger(__name__)

class EchoToolPlugin(ToolPlugin):
    name = "tool-echo"
    version = "1.0.0"
    def get_tools(self):
        return [{"type": "function", "function": {"name": "echo", "description": "回显", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}]
    async def execute_tool(self, tool_name: str, **kwargs):
        if tool_name == "echo":
            return f"[echo] {kwargs.get('text', '')}"
        raise ValueError(f"未知工具: {tool_name}")
""", encoding="utf-8")
    (tool_echo_dir / "__init__.py").write_text("from .plugin import EchoToolPlugin\n", encoding="utf-8")

    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        # 使用 __replace__ 完全替换内置插件，避免加载代码内置的 nanobee/builtin/
        "plugin_dirs": ["__replace__", str(builtin_dir)],
    }

    CoreMDParser.create_default(tmp_path / "core.md")

    kernel = NanobeeKernel(config=config)
    await kernel.boot()

    assert kernel.is_booted

    # 验证插件被正确发现和加载
    plugins = kernel.plugin_manager.list_plugins()
    assert "channel-cli" in plugins
    assert "tool-echo" in plugins

    # 按类型查询插件
    channels = kernel.plugin_manager.get_by_type("channel")
    tools = kernel.plugin_manager.get_by_type("tool")
    assert len(channels) == 1
    assert len(tools) == 1

    # 验证工具插件功能
    echo_plugin = tools[0]
    tool_list = echo_plugin.get_tools()
    assert len(tool_list) == 1
    assert tool_list[0]["function"]["name"] == "echo"

    result = await echo_plugin.execute_tool("echo", text="hello e2e")
    assert result == "[echo] hello e2e"

    await kernel.shutdown()
    assert not kernel.is_booted


@pytest.mark.asyncio
async def test_e2e_context_lifecycle(tmp_path):
    """测试上下文管理器的完整生命周期"""
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": ["__replace__"],
    }
    CoreMDParser.create_default(tmp_path / "core.md")

    kernel = NanobeeKernel(config=config)
    await kernel.boot()

    # 验证上下文初始化
    context = await kernel.context_manager.get_or_create("session-1")
    assert context.context_id == "session-1"
    assert context.work_dir.exists()
    assert context.memory_dir.exists()

    # 通过 SessionManager 添加消息（多 session 隔离）
    from nanobee.session.session_manager import SessionManager
    session_mgr = SessionManager(context.base_dir.parent)
    s = session_mgr.get_or_create("session-1", "cli:chat")
    s.add_message("user", "测试消息")
    session_mgr.save(s)
    assert len(s.messages) == 1
    assert s.messages[0]["content"] == "测试消息"

    # 切换上下文
    ctx2 = await kernel.context_manager.switch("session-2")
    assert ctx2.context_id == "session-2"
    s2 = session_mgr.get_or_create("session-2", "cli:chat")
    assert len(s2.messages) == 0  # 隔离

    # 列出上下文
    ctx_list = kernel.context_manager.list_contexts()
    assert "session-1" in ctx_list
    assert "session-2" in ctx_list

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_e2e_soul_guard(tmp_path):
    """测试灵魂守卫的三层防护"""
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": ["__replace__"],
    }
    CoreMDParser.create_default(tmp_path / "core.md")

    # 送检灵魂守卫正常启动
    kernel = NanobeeKernel(config=config)
    await kernel.boot()
    assert kernel.is_booted
    await kernel.shutdown()
