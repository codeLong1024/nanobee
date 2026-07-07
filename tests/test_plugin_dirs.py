"""resolve_plugin_dirs 单元测试 — 插件目录解析。

覆盖场景：
1. 显式指定 plugin_dirs（带/不带 __replace__）
2. 配置文件指定 config_dirs
3. 默认自动发现 <data_dir>/plugins/
4. 内置插件始终在最前
5. 相对路径基于 data_dir 解析
6. 空列表语义
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.kernel.plugin_dirs import resolve_plugin_dirs


class TestResolvePluginDirs:
    """resolve_plugin_dirs() 测试。"""

    def test_default_auto_discovery_with_existing_dir(self, tmp_path: Path) -> None:
        """<data_dir>/plugins/ 存在时自动发现。"""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
        )
        assert "/builtin" in result
        assert str(plugins_dir) in result

    def test_default_auto_discovery_without_dir(self, tmp_path: Path) -> None:
        """<data_dir>/plugins/ 不存在时不报错，仅返回内置。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
        )
        assert result == ["/builtin"]

    def test_explicit_plugin_dirs_preserves_builtin(self, tmp_path: Path) -> None:
        """显式 plugin_dirs 不覆盖内置，内置在前。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["/instance/plugins"],
        )
        assert result == ["/builtin", "/instance/plugins"]

    def test_replace_special_dir_removes_builtin(self, tmp_path: Path) -> None:
        """__replace__ 特殊目录移除内置插件。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["__replace__", "/instance/plugins"],
        )
        assert "/builtin" not in result
        assert result == ["/instance/plugins"]

    def test_config_dirs_fallback(self, tmp_path: Path) -> None:
        """无 plugin_dirs 时使用 config_dirs。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            config_dirs=["/config/plugins"],
        )
        assert result == ["/builtin", "/config/plugins"]

    def test_empty_list_disables_all(self, tmp_path: Path) -> None:
        """空列表不加载任何插件。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=[],
        )
        assert result == []

    def test_relative_path_resolved_to_data_dir(self, tmp_path: Path) -> None:
        """相对路径基于 data_dir 解析。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["relative/plugins"],
        )
        expected = str(tmp_path / "relative/plugins")
        assert expected in result

    def test_absolute_path_preserved(self, tmp_path: Path) -> None:
        """绝对路径保持不变。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["/absolute/path"],
        )
        assert "/absolute/path" in result

    def test_multiple_plugin_dirs(self, tmp_path: Path) -> None:
        """多个插件目录均被包含。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["/dir1", "/dir2"],
        )
        assert result == ["/builtin", "/dir1", "/dir2"]

    def test_plugin_dirs_overrides_config_dirs(self, tmp_path: Path) -> None:
        """plugin_dirs 优先级高于 config_dirs。"""
        result = resolve_plugin_dirs(
            data_dir=tmp_path,
            package_builtin="/builtin",
            plugin_dirs=["/explicit"],
            config_dirs=["/config"],
        )
        assert "/explicit" in result
        assert "/config" not in result
