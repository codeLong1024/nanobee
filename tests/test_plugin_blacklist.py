"""测试通过 blacklist 禁用内置插件"""

from __future__ import annotations

import pytest

from nanobee.kernel.core_parser import CoreMDParser
from nanobee.kernel import NanobeeKernel


@pytest.mark.asyncio
async def test_blacklist_disables_builtin_plugin(tmp_path):
    """通过 blacklist 禁用内置插件"""
    # 使用 __replace__ 加载测试插件，然后用 blacklist 禁用
    test_builtin = tmp_path / "test_builtin"
    tool_shell_dir = test_builtin / "tool_shell"
    tool_shell_dir.mkdir(parents=True)
    (tool_shell_dir / "plugin.toml").write_text("""[plugin]
name = "tool_shell"
version = "1.0.0"
description = "Shell 工具"
author = "test"
type = "tool"
""", encoding="utf-8")
    (tool_shell_dir / "plugin.py").write_text("""from nanobee.plugins.tool import ToolPlugin
class ShellPlugin(ToolPlugin):
    name = "tool_shell"
    version = "1.0.0"
    def get_tools(self): return []
    async def execute_tool(self, tool_name, **kwargs): return ""
""", encoding="utf-8")
    (tool_shell_dir / "__init__.py").write_text("from .plugin import ShellPlugin\n", encoding="utf-8")
    
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": ["__replace__", str(test_builtin)],  # 只加载测试插件
        "agents": {
            "defaults": {
                "blacklist": ["tool_shell"],  # 禁用 tool_shell
            }
        },
    }
    
    CoreMDParser.create_default(tmp_path / "core.md")
    
    kernel = NanobeeKernel(config=config)
    await kernel.boot()
    
    # 验证 tool_shell 被 blacklist 禁用
    plugins = kernel.plugin_manager.list_plugins()
    assert "tool_shell" not in plugins, "tool_shell 应该被 blacklist 禁用"


@pytest.mark.asyncio
async def test_channels_enabled_false_disables_plugin(tmp_path):
    """通过 channels.<name>.enabled: false 禁用通道插件"""
    # 使用 __replace__ 加载测试插件
    test_builtin = tmp_path / "test_builtin"
    
    # 创建 channel_http 插件
    http_dir = test_builtin / "channel_http"
    http_dir.mkdir(parents=True)
    (http_dir / "plugin.toml").write_text("""[plugin]
name = "channel_http"
version = "1.0.0"
description = "HTTP 通道"
author = "test"
type = "channel"
""", encoding="utf-8")
    (http_dir / "plugin.py").write_text("""from nanobee.channel.base import ChannelPlugin
class HTTPPlugin(ChannelPlugin):
    name = "channel_http"
    version = "1.0.0"
    async def start(self): pass
    async def stop(self): pass
    async def send(self, message, context_id="default"): pass
    async def _process_incoming(self, message, context_manager): return []
""", encoding="utf-8")
    (http_dir / "__init__.py").write_text("from .plugin import HTTPPlugin\n", encoding="utf-8")
    
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": ["__replace__", str(test_builtin)],  # 只加载测试插件
        "channels": {
            "channel_http": {
                "enabled": False,  # 禁用 HTTP 通道
            }
        },
    }
    
    CoreMDParser.create_default(tmp_path / "core.md")
    
    kernel = NanobeeKernel(config=config)
    await kernel.boot()
    
    # 验证 channel_http 被 enabled: false 禁用
    plugins = kernel.plugin_manager.list_plugins()
    assert "channel_http" not in plugins, "channel_http 应该被 enabled: false 禁用"
