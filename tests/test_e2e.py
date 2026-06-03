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
from nanobee.plugins.channel import ChannelPlugin

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
    async def send(self, message: str, **kwargs) -> None:
        print(f"CLI: {message}")
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
from nanobee.plugins.tool import ToolPlugin

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
        "work_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": [str(builtin_dir)],
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
async def test_e2e_memory_file_plugin(tmp_path):
    """测试 memory-file 插件的基本读写操作"""
    import importlib
    _mod = importlib.import_module("nanobee.builtin.memory_file.plugin")
    MemoryFilePlugin = _mod.MemoryFilePlugin
    del importlib, _mod

    meta = tmp_path / "meta"
    meta.mkdir()

    plugin = MemoryFilePlugin()
    plugin.initialize({"work_dir": str(tmp_path)})

    # 存储和检索
    await plugin.store("test-key", "test-value", memory_type="test")
    retrieved = await plugin.retrieve("test-key")
    assert retrieved == "test-value"

    # 搜索
    await plugin.store("another", "hello world")
    results = await plugin.search("world")
    assert len(results) >= 1

    # 列出
    keys = await plugin.list_all(memory_type="test")
    assert "test-key" in keys

    # 删除
    deleted = await plugin.delete("test-key")
    assert deleted is True
    assert await plugin.retrieve("test-key") is None


@pytest.mark.asyncio
async def test_e2e_context_lifecycle(tmp_path):
    """测试上下文管理器的完整生命周期"""
    config = {
        "work_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
    }
    CoreMDParser.create_default(tmp_path / "core.md")

    kernel = NanobeeKernel(config=config)
    await kernel.boot()

    # 验证上下文初始化
    context = await kernel.context_manager.get_or_create("session-1")
    assert context.context_id == "session-1"
    assert context.work_dir.exists()
    assert context.memory_dir.exists()

    # 添加消息
    context.add_message("user", "测试消息")
    msgs = context.get_messages()
    assert len(msgs) == 1
    assert msgs[0]["content"] == "测试消息"

    # 切换上下文
    ctx2 = await kernel.context_manager.switch("session-2")
    assert ctx2.context_id == "session-2"
    assert len(ctx2.get_messages()) == 0  # 隔离

    # 列出上下文
    ctx_list = kernel.context_manager.list_contexts()
    assert "session-1" in ctx_list
    assert "session-2" in ctx_list

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_e2e_soul_guard(tmp_path):
    """测试灵魂守卫的三层防护"""
    config = {
        "work_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
    }
    CoreMDParser.create_default(tmp_path / "core.md")

    # 送检灵魂守卫正常启动
    kernel = NanobeeKernel(config=config)
    await kernel.boot()
    assert kernel.is_booted
    await kernel.shutdown()
