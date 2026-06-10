"""插件 enabled 覆盖测试（框架无知论方案 B）

验证 config.yaml 的 plugins.<name>.enabled 可以覆盖 plugin.toml 中的默认值，
实现"不同实例加载不同插件组合"的需求。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobee.config.schema import Config
from nanobee.kernel.kernel import NanobeeKernel
from nanobee.kernel.plugin_manager import PluginManager


def _make_descriptor(name: str, enabled: bool = True) -> MagicMock:
    """创建模拟的 PluginDescriptor"""
    desc = MagicMock()
    desc.metadata.name = name
    desc.config = {"enabled": enabled}
    return desc


@pytest.fixture
def temp_core_md(tmp_path):
    """创建临时 core.md 文件"""
    from nanobee.kernel.core_parser import CoreMDParser
    CoreMDParser.create_default(tmp_path / "core.md")
    return tmp_path / "core.md"


@pytest.mark.asyncio
async def test_enabled_override_false_disables_plugin(temp_core_md, tmp_path):
    """配置 plugins.foo.enabled=false 应阻止插件启用"""
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        plugins={"foo": {"enabled": False}},
    )
    kernel = NanobeeKernel(config=config)

    # 模拟 PluginManager：只返回 "foo" 这一插件，plugin.toml 认为启用
    mock_pm = MagicMock(spec=PluginManager)
    mock_pm.list_plugins.return_value = ["foo"]
    mock_pm.get_descriptor.return_value = _make_descriptor("foo", enabled=True)
    kernel.plugin_manager = mock_pm

    await kernel.boot()

    # enable 不应被调用（config.yaml 的 enabled=false 覆盖了 plugin.toml 的 enabled=true）
    mock_pm.enable.assert_not_called()
    assert kernel.is_booted

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_enabled_override_true_enables_plugin(temp_core_md, tmp_path):
    """配置 plugins.foo.enabled=true 应允许插件启用"""
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        plugins={"foo": {"enabled": True}},
    )
    kernel = NanobeeKernel(config=config)

    mock_pm = MagicMock(spec=PluginManager)
    mock_pm.list_plugins.return_value = ["foo"]
    mock_pm.get_descriptor.return_value = _make_descriptor("foo", enabled=True)
    kernel.plugin_manager = mock_pm

    await kernel.boot()

    mock_pm.enable.assert_called_once_with("foo")
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_enabled_not_in_config_falls_back_to_plugin_toml(temp_core_md, tmp_path):
    """config.yaml 未配置 enabled 时，回退到 plugin.toml 的 enabled"""
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        plugins={},
    )
    kernel = NanobeeKernel(config=config)

    # plugin.toml 中 enabled=true
    mock_pm = MagicMock(spec=PluginManager)
    mock_pm.list_plugins.return_value = ["foo"]
    mock_pm.get_descriptor.return_value = _make_descriptor("foo", enabled=True)
    kernel.plugin_manager = mock_pm

    await kernel.boot()
    mock_pm.enable.assert_called_once_with("foo")
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_enabled_not_in_config_and_plugin_toml_false_disables(temp_core_md, tmp_path):
    """plugin.toml 中 enabled=false 应阻止启用（无 config.yaml 覆盖时）"""
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        plugins={},
    )
    kernel = NanobeeKernel(config=config)

    mock_pm = MagicMock(spec=PluginManager)
    mock_pm.list_plugins.return_value = ["bar"]
    mock_pm.get_descriptor.return_value = _make_descriptor("bar", enabled=False)
    kernel.plugin_manager = mock_pm

    await kernel.boot()
    mock_pm.enable.assert_not_called()
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_multiple_plugins_mixed_enabled(temp_core_md, tmp_path):
    """混合场景：config.yaml 禁用部分插件，其他保持默认"""
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        plugins={
            "tool_shell": {"enabled": False},
            # tool_echo 不禁用
        },
    )
    kernel = NanobeeKernel(config=config)

    foo_desc = _make_descriptor("tool_shell", enabled=True)   # plugin.toml 允许
    bar_desc = _make_descriptor("tool_echo", enabled=True)     # plugin.toml 允许

    mock_pm = MagicMock(spec=PluginManager)
    mock_pm.list_plugins.return_value = ["tool_shell", "tool_echo"]
    mock_pm.get_descriptor.side_effect = lambda name: {"tool_shell": foo_desc, "tool_echo": bar_desc}[name]
    kernel.plugin_manager = mock_pm

    await kernel.boot()

    # tool_shell 被 config.yaml 禁用，不应启用
    # tool_echo 未被 config.yaml 禁用，应启用
    mock_pm.enable.assert_called_once_with("tool_echo")
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_enabled_override_with_real_plugin_manager(temp_core_md, tmp_path):
    """集成测试：使用真实 PluginManager + 配置 enabled 覆盖

    验证 boot() 中从 config.plugins 读取 enabled 的完整链路。
    使用真实 PluginManager（但插件目录为空，无插件可加载，不报错即可）。
    """
    config = Config(
        work_dir=str(tmp_path),
        core_md_path=str(temp_core_md),
        # plugins 不为空，但实际无插件会被加载（空目录），不会触发错误
        plugins={"tool_shell": {"enabled": False}},
        # 空的插件目录，避免扫描到真实插件
        plugin_dirs=[],
    )
    kernel = NanobeeKernel(config=config)

    await kernel.boot()
    assert kernel.is_booted
    await kernel.shutdown()
