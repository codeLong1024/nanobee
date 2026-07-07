"""插件体系单元测试"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobee.kernel.context_pipeline import ContextPipeline, _map_plugin_stage
from nanobee.kernel.plugin_manager import PluginManager, PluginDescriptor
from nanobee.kernel.skill_manager import SkillsLoader
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


# =============================================================================
# Hook 机制测试（从 Phase 2 验收测试迁移）
# =============================================================================


# ---- Hook 测试用插件 ----

class _TestMemoryPlugin(NanobeePlugin):
    """模拟记忆插件，返回固定的记忆内容。"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="test_memory", plugin_type="memory")
        super().__init__(metadata)

    def contribute_to_prompt(self, context) -> str | None:
        return "这是 Alice 的记忆内容"


class _TestSkillPlugin(NanobeePlugin):
    """模拟技能插件，注入技能描述。"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="test_skill", plugin_type="skill")
        super().__init__(metadata)

    def contribute_to_prompt(self, context) -> str | None:
        return "可用技能：web-search, calc"


class _TestToolAddPlugin(NanobeePlugin):
    """模拟插件，动态添加工具。"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="test_tool_add", plugin_type="tool_add")
        super().__init__(metadata)

    def contribute_to_tools(self, context, current_tool_names):
        return current_tool_names + ["tool-web", "tool-calc"]


class _TestPostInvokePlugin(NanobeePlugin):
    """模拟插件，修改工具返回结果。"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="test_post_invoke", plugin_type="tool")
        super().__init__(metadata)

    async def on_post_invoke(self, context, tool_name, result):
        if tool_name == "test_tool":
            return f"插件修改: {result}"
        return result


class _TestMessageCompletedPlugin(NanobeePlugin):
    """模拟插件，记录消息完成事件。"""

    def __init__(self, metadata=None):
        if metadata is None:
            metadata = PluginMetadata(name="test_msg_completed", plugin_type="tool")
        super().__init__(metadata)
        self.completed_messages = []

    async def on_message_completed(self, context, messages):
        self.completed_messages = list(messages)


# ---- 辅助工具 ----

def _make_context_pipeline(tmp_path: Path) -> ContextPipeline:
    """创建带有效 core.md 的 ContextPipeline。"""
    core_md = tmp_path / "core.md"
    core_md.write_text(
        "# Test\n\n## Soul\nTest personality\n\n## Rules\nBe helpful.\n",
        encoding="utf-8",
    )
    return ContextPipeline(
        core_md_path=str(core_md),
        skill_loader=SkillsLoader(tmp_path / "skills"),
    )


class _FakeUserContext:
    """模拟用户上下文。"""

    def __init__(self, user_id: str = "test-user"):
        self.user_id = user_id


class TestPluginHookMixin:
    """验证 PluginHookMixin 基本功能（通过 NanobeePlugin 继承链）。"""

    def test_mixin_importable(self):
        """PluginHookMixin 可导入。"""
        from nanobee.plugins.hook_mixin import PluginHookMixin as PHM
        assert PHM is not None

    def test_mixin_default_implementations(self):
        """混入类的默认实现不报错。"""
        plugin = _TestPostInvokePlugin()
        assert plugin.contribute_to_prompt(None) is None
        assert plugin.contribute_to_tools(None, ["a"]) == ["a"]

    @pytest.mark.asyncio
    async def test_mixin_async_defaults(self):
        """混入类的异步默认实现不报错。"""
        plugin = _TestPostInvokePlugin()
        result = await plugin.on_pre_invoke(None, "test", {"k": "v"})
        assert result == {"k": "v"}
        result = await plugin.on_post_invoke(None, "test", "ok")
        assert result == "ok"
        await plugin.on_message_completed(None, [])


class TestNanobeePluginHookMethods:
    """验证 NanobeePlugin 基类的默认 Hook 方法。"""

    def test_base_has_hook_methods(self):
        """NanobeePlugin 包含 5 个 Hook 默认方法。"""
        plugin = NanobeePlugin.__new__(NanobeePlugin)
        assert hasattr(plugin, "contribute_to_prompt")
        assert hasattr(plugin, "contribute_to_tools")
        assert hasattr(plugin, "on_pre_invoke")
        assert hasattr(plugin, "on_post_invoke")
        assert hasattr(plugin, "on_message_completed")

    def test_subclass_override(self):
        """子类覆盖的方法被正确调用。"""
        plugin = _TestMemoryPlugin()
        result = plugin.contribute_to_prompt(MagicMock())
        assert result == "这是 Alice 的记忆内容"


class TestContextPipelineWithPlugins:
    """验证 ContextPipeline.build_with_plugins()。"""

    @pytest.mark.asyncio
    async def test_build_with_no_plugins(self, tmp_path: Path):
        """无插件时行为同 build()。"""
        pipeline = _make_context_pipeline(tmp_path)
        result = await pipeline.build_with_plugins(
            {"system_prompt": ""}, _FakeUserContext(), [],
        )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_memory_plugin_contributes_to_prompt(self, tmp_path: Path):
        """记忆插件通过 contribute_to_prompt 注入内容。"""
        pipeline = _make_context_pipeline(tmp_path)
        plugin = _TestMemoryPlugin()
        result = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你好\n"}, _FakeUserContext(), [plugin],
        )
        assert "这是 Alice 的记忆内容" in result

    @pytest.mark.asyncio
    async def test_multiple_plugins_ordered(self, tmp_path: Path):
        """多个插件按类型分组。"""
        pipeline = _make_context_pipeline(tmp_path)
        plugins = [_TestMemoryPlugin(), _TestSkillPlugin()]
        result = await pipeline.build_with_plugins(
            {"system_prompt": "## Soul\n你好\n"}, _FakeUserContext(), plugins,
        )
        assert "这是 Alice 的记忆内容" in result
        assert "可用技能：web-search, calc" in result
        assert "## memory" in result
        assert "## skill" in result


class TestPluginToolContribution:
    """验证 contribute_to_tools 集成。"""

    def test_plugin_adds_tools(self):
        """插件通过 contribute_to_tools 添加工具。"""
        plugin = _TestToolAddPlugin()
        modified = plugin.contribute_to_tools(MagicMock(), ["tool-fs", "tool-echo"])
        assert "tool-web" in modified
        assert "tool-calc" in modified
        assert len(modified) == 4


class TestPluginPostInvoke:
    """验证 on_post_invoke 修改工具结果。"""

    @pytest.mark.asyncio
    async def test_post_invoke_modifies_result(self):
        """插件通过 on_post_invoke 修改工具返回结果。"""
        plugin = _TestPostInvokePlugin()
        result = await plugin.on_post_invoke(MagicMock(), "test_tool", "原始结果")
        assert result == "插件修改: 原始结果"

    @pytest.mark.asyncio
    async def test_post_invoke_other_tool_unchanged(self):
        """无关工具的结果不被修改。"""
        plugin = _TestPostInvokePlugin()
        result = await plugin.on_post_invoke(MagicMock(), "other_tool", "其他结果")
        assert result == "其他结果"


class TestPluginMessageCompleted:
    """验证 on_message_completed 被调用。"""

    @pytest.mark.asyncio
    async def test_message_completed_receives_messages(self):
        """插件收到完成通知和消息列表。"""
        plugin = _TestMessageCompletedPlugin()
        test_messages = [{"role": "user", "content": "hi"}]
        await plugin.on_message_completed(MagicMock(), test_messages)
        assert len(plugin.completed_messages) > 0
        assert plugin.completed_messages[0]["content"] == "hi"


class TestPluginStageMapping:
    """验证 _map_plugin_stage 工具函数。"""

    def test_memory_type_maps_to_memory_section(self):
        """plugin_type=memory → '## memory'。"""
        assert _map_plugin_stage(_TestMemoryPlugin()) == "## memory"

    def test_skill_type_maps_to_skill_section(self):
        """plugin_type=skill → '## skill'。"""
        assert _map_plugin_stage(_TestSkillPlugin()) == "## skill"

    def test_unknown_type_maps_to_type_name(self):
        """未知类型 → '## {plugin_type}'。"""
        assert _map_plugin_stage(_TestToolAddPlugin()) == "## tool_add"


class TestPluginHookMixinComposition:
    """验证 PluginHookMixin 与 NanobeePlugin 的继承关系。"""

    def test_nanobee_plugin_inherits_mixin(self):
        """NanobeePlugin 继承 PluginHookMixin。"""
        from nanobee.plugins.hook_mixin import PluginHookMixin as PHM
        plugin = _TestToolAddPlugin()
        assert isinstance(plugin, PHM)
        assert isinstance(plugin, NanobeePlugin)

    def test_on_message_completed_handles_plugin_list(self):
        """AgentLoop._notify_plugins_message_completed 方法签名正确。"""
        from nanobee.agent.loop import AgentLoop
        sig = inspect.signature(AgentLoop._notify_plugins_message_completed)
        params = list(sig.parameters.keys())
        assert "context_id" in params
        assert "messages" in params
