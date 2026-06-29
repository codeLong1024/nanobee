"""插件体系单元测试"""

from __future__ import annotations

import pytest

from nanobee.kernel.plugin_manager import PluginManager, PluginDescriptor
from nanobee.plugins.base import NanobeePlugin, PluginMetadata
from nanobee.plugins import ToolPlugin
from nanobee.channel.base import ChannelPlugin
from nanobee.plugins.memory import MemoryPlugin


class MockToolPlugin(ToolPlugin):
    """测试用工具插件"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="mock-tool", plugin_type="tool")
        super().__init__(metadata)

    def get_tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显输入",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            }
        }]

    async def execute_tool(self, tool_name, **kwargs):
        if tool_name == "echo":
            return kwargs.get("text", "")
        raise ValueError(f"Unknown tool: {tool_name}")


class MockChannelPlugin(ChannelPlugin):
    """测试用通道插件"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="mock-channel", plugin_type="channel")
        super().__init__(metadata)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, message, context_id="default"):
        from nanobee.channel.message import OutboundMessage
        if isinstance(message, str):
            message = OutboundMessage(
                channel=self.metadata.name,
                chat_id=context_id.split(":", 1)[-1],
                content=message,
            )
        self.last_message = message.content if hasattr(message, "content") else str(message)

    async def _process_incoming(
        self,
        message,
        context_manager,
    ):
        return []


def test_plugin_metadata():
    """测试插件元数据"""
    meta = PluginMetadata(
        name="test-plugin",
        version="1.0.0",
        plugin_type="tool",
    )
    assert meta.name == "test-plugin"
    assert meta.version == "1.0.0"


def test_tool_plugin_interface():
    """测试 ToolPlugin 接口"""
    plugin = MockToolPlugin()
    assert plugin.plugin_type == "tool"
    tools = plugin.get_tools()
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "echo"


@pytest.mark.asyncio
async def test_tool_plugin_execute():
    """测试工具插件执行"""
    plugin = MockToolPlugin()
    result = await plugin.execute_tool("echo", text="hello")
    assert result == "hello"


def test_channel_plugin_interface():
    """测试 ChannelPlugin 接口"""
    plugin = MockChannelPlugin()
    assert plugin.plugin_type == "channel"


@pytest.mark.asyncio
async def test_channel_plugin_send():
    """测试通道插件发送"""
    plugin = MockChannelPlugin()
    await plugin.send("test message")
    assert plugin.last_message == "test message"


def test_list_plugins():
    """测试 PluginManager 列表功能"""
    manager = PluginManager(kernel=None)
    assert manager.list_plugins() == []


def test_plugin_default_metadata():
    """测试插件默认元数据"""
    plugin = MockToolPlugin()
    assert plugin.metadata.name == "mock-tool"
    assert plugin.metadata.plugin_type == "tool"


def test_plugin_lifecycle():
    """测试插件生命周期"""
    plugin = MockToolPlugin()
    assert not plugin.is_enabled

    plugin.on_enable()
    assert plugin.is_enabled

    plugin.on_disable()
    assert not plugin.is_enabled


def test_get_by_type_empty():
    """测试按类型获取（空）"""
    manager = PluginManager(kernel=None)
    result = manager.get_by_type("tool")
    assert result == []


@pytest.mark.asyncio
async def test_tool_list_tool_names():
    """测试工具名称列表"""
    plugin = MockToolPlugin()
    names = plugin.list_tool_names()
    assert names == ["echo"]


def test_plugin_config_isolation():
    """测试插件配置隔离：每个插件只能读取自己的配置"""
    plugin = MockToolPlugin()
    meta = PluginMetadata(name="mock-tool", plugin_type="tool")
    plugin._metadata = meta

    # 模拟 kernel 配置（包含多个插件的配置）
    class MockKernel:
        config = {
            "plugins": {
                "mock-tool": {"key1": "value1", "key2": "value2"},
                "other-plugin": {"secret": "should_not_access"},
            }
        }

    plugin.initialize(MockKernel())

    # 插件只能读取自己的配置
    assert plugin.get_config("key1") == "value1"
    assert plugin.get_config("key2") == "value2"
    assert plugin.get_config("nonexistent", "default") == "default"

    # 插件无法读取其他插件的配置（通过 get_config 接口）
    assert plugin.get_config("secret") is None


def test_plugin_config_empty_when_no_kernel():
    """测试无 kernel 时配置为空"""
    plugin = MockToolPlugin()
    plugin.initialize(None)
    assert plugin.get_config("any_key") is None
    assert plugin._config == {}


def test_plugin_config_copied_not_referenced():
    """测试配置是拷贝而非引用，防止外部修改"""
    plugin = MockToolPlugin()
    meta = PluginMetadata(name="mock-tool", plugin_type="tool")
    plugin._metadata = meta

    test_config = {"plugins": {"mock-tool": {"key": "value"}}}

    class MockKernel:
        config = test_config

    plugin.initialize(MockKernel())

    # 修改原始配置不应影响插件
    test_config["plugins"]["mock-tool"]["key"] = "modified"
    assert plugin.get_config("key") == "value"
