"""CLI plugin 单元测试"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobee.cli.plugin import (
    _discover_plugins,
    _format_plugin_table,
    _resolve_plugin_dirs,
)
from nanobee.config.loader import load_config
from nanobee.kernel.plugin_manager import PluginDescriptor


def test_resolve_plugin_dirs_with_config():
    """测试使用配置文件中的插件目录"""
    cfg = load_config()
    cfg.plugin_dirs = ["/custom/plugins", "/another/path"]
    dirs = _resolve_plugin_dirs(cfg)
    assert len(dirs) == 2
    assert dirs[0] == Path("/custom/plugins").resolve()
    assert dirs[1] == Path("/another/path").resolve()


def test_resolve_plugin_dirs_empty():
    """测试空配置时使用自动检测"""
    cfg = load_config()
    cfg.plugin_dirs = []
    dirs = _resolve_plugin_dirs(cfg)
    # 应该自动检测到 nanobee/builtin 或 builtin
    assert len(dirs) > 0
    for d in dirs:
        assert d.is_absolute()


def test_format_plugin_table_empty():
    """测试空插件列表的格式化"""
    result = _format_plugin_table([])
    assert "未找到已安装的插件" in result


def test_format_plugin_table_with_plugins():
    """测试有插件时的格式化"""
    plugins = [
        {
            "name": "tool_echo",
            "version": "1.0.0",
            "description": "回显测试工具",
            "type": "tool",
            "enabled": True,
            "path": "/test/path",
        },
        {
            "name": "channel_cli",
            "version": "1.0.0",
            "description": "命令行通道",
            "type": "channel",
            "enabled": False,
            "path": "/test/path2",
        },
    ]
    result = _format_plugin_table(plugins)
    assert "tool_echo" in result
    assert "channel_cli" in result
    assert "共 2 个插件" in result
    assert "enabled" in result
    assert "disabled" in result


def test_discover_plugins_with_real_plugin():
    """测试使用真实插件目录扫描"""
    # 使用 nanobee/builtin 目录
    builtin_dir = Path(__file__).parent.parent / "nanobee" / "builtin"
    if not builtin_dir.exists():
        pytest.skip("builtin directory not found")

    plugins = _discover_plugins([builtin_dir])
    # 应该至少找到一个插件
    assert len(plugins) > 0
    # 验证插件结构
    tool_echo = next((p for p in plugins if p["name"] == "tool_echo"), None)
    assert tool_echo is not None
    assert tool_echo["type"] == "tool"
    assert "version" in tool_echo
    assert "description" in tool_echo
    assert "enabled" in tool_echo
    assert "path" in tool_echo


def test_discover_plugins_empty_directory(tmp_path: Path) -> None:
    """测试扫描空目录"""
    plugins = _discover_plugins([tmp_path])
    assert plugins == []


def test_discover_plugins_invalid_directory(tmp_path: Path) -> None:
    """测试扫描不存在的目录"""
    nonexistent = tmp_path / "nonexistent"
    plugins = _discover_plugins([nonexistent])
    assert plugins == []


def test_plugin_descriptor_discover(tmp_path: Path) -> None:
    """测试 PluginDescriptor 发现插件"""
    plugin_dir = tmp_path / "test_plugin"
    plugin_dir.mkdir()

    # 创建 plugin.toml
    toml_content = """
[plugin]
name = "test_plugin"
version = "1.0.0"
description = "测试插件"
type = "tool"

[config]
enabled = true
"""
    (plugin_dir / "plugin.toml").write_text(toml_content, encoding="utf-8")

    # 创建 plugin.py
    (plugin_dir / "plugin.py").write_text("# placeholder", encoding="utf-8")

    desc = PluginDescriptor.discover(plugin_dir)
    assert desc is not None
    assert desc.metadata.name == "test_plugin"
    assert desc.metadata.version == "1.0.0"
    assert desc.metadata.description == "测试插件"
    assert desc.metadata.plugin_type == "tool"
    # config 是 plugin.toml 中顶层的 config 段
    assert "enabled" in desc.config


def test_plugin_descriptor_missing_toml(tmp_path: Path) -> None:
    """测试缺少 plugin.toml 时返回 None"""
    plugin_dir = tmp_path / "no_toml_plugin"
    plugin_dir.mkdir()

    desc = PluginDescriptor.discover(plugin_dir)
    assert desc is None


@pytest.fixture
def cli_runner():
    """创建 Click CLI 测试客户端"""
    from click.testing import CliRunner
    from nanobee.cli.main import main

    return CliRunner(), main


def test_plugin_list_command(cli_runner):
    """测试 plugin list 命令"""
    runner, main = cli_runner
    result = runner.invoke(main, ["plugin", "list"])
    assert result.exit_code == 0
    assert "共" in result.output or "未找到" in result.output


def test_plugin_list_command_json(cli_runner):
    """测试 plugin list --json 命令"""
    runner, main = cli_runner
    result = runner.invoke(main, ["plugin", "list", "--json"])
    assert result.exit_code == 0
    # JSON 输出应该可以被解析
    try:
        plugins = json.loads(result.output)
        assert isinstance(plugins, list)
    except json.JSONDecodeError:
        # 如果输出为空，也认为是有效的
        assert result.output.strip() == ""


def test_plugin_create_command(cli_runner, tmp_path: Path):
    """测试 plugin create 命令"""
    runner, main = cli_runner

    # 切换到临时目录
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(main, ["plugin", "create", "my_new_plugin"])
        assert result.exit_code == 0
        assert "插件已创建" in result.output

        # 验证创建的文件
        plugin_dir = tmp_path / "plugins" / "my_new_plugin"
        assert plugin_dir.exists()
        assert (plugin_dir / "plugin.toml").exists()
        assert (plugin_dir / "plugin.py").exists()
        assert (plugin_dir / "__init__.py").exists()
    finally:
        os.chdir(old_cwd)


def test_plugin_create_command_invalid_name(cli_runner):
    """测试 plugin create 命令的无效名称"""
    runner, main = cli_runner

    result = runner.invoke(main, ["plugin", "create", "invalid-name"])
    assert result.exit_code != 0
    assert "错误" in result.output


def test_plugin_create_command_duplicate(cli_runner, tmp_path: Path):
    """测试 plugin create 命令的重复创建"""
    runner, main = cli_runner

    # 切换到临时目录
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # 第一次创建
        result1 = runner.invoke(main, ["plugin", "create", "my_plugin"])
        assert result1.exit_code == 0

        # 第二次创建（应该失败）
        result2 = runner.invoke(main, ["plugin", "create", "my_plugin"])
        assert result2.exit_code != 0
        assert "已存在" in result2.output
    finally:
        os.chdir(old_cwd)


def test_plugin_enable_disable_commands(cli_runner):
    """测试 plugin enable/disable 命令"""
    runner, main = cli_runner

    # 测试 enable
    result = runner.invoke(main, ["plugin", "enable", "test_plugin"])
    assert result.exit_code == 0
    assert "启用插件: test_plugin" in result.output

    # 测试 disable
    result = runner.invoke(main, ["plugin", "disable", "test_plugin"])
    assert result.exit_code == 0
    assert "禁用插件: test_plugin" in result.output


def test_plugin_help_command(cli_runner):
    """测试 plugin --help 命令"""
    runner, main = cli_runner
    result = runner.invoke(main, ["plugin", "--help"])
    assert result.exit_code == 0
    assert "插件管理命令" in result.output
    assert "list" in result.output
    assert "create" in result.output
    assert "enable" in result.output
    assert "disable" in result.output


def test_plugin_list_help(cli_runner):
    """测试 plugin list --help 命令"""
    runner, main = cli_runner
    result = runner.invoke(main, ["plugin", "list", "--help"])
    assert result.exit_code == 0
    assert "列出已安装的插件" in result.output
    assert "--config" in result.output
    assert "--json" in result.output
