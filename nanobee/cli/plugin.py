"""
Nanobee Plugin - Plugin 子命令

提供插件管理 CLI 命令：list、enable、disable、create。
通过扫描配置中的插件目录来发现已安装的插件，无需启动内核。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import click

from nanobee.config.loader import load_config


def _resolve_plugin_dirs(cfg: Any) -> list[Path]:
    """解析插件目录列表。

    优先级：配置文件中的 plugin_dirs > 自动检测 > 默认值 ["builtin", "plugins"]。
    自动检测会查找当前工作目录和包目录（nanobee/builtin）下的插件目录。

    Args:
        cfg: 配置对象

    Returns:
        插件目录路径列表
    """
    raw_dirs: list[str] = []
    if hasattr(cfg, "plugin_dirs"):
        raw_dirs = list(cfg.plugin_dirs)

    if raw_dirs:
        # 用户配置了插件目录，使用用户配置
        return [Path(d).resolve() if Path(d).is_absolute() else Path(d).resolve() for d in raw_dirs]

    # 自动检测：查找常见位置的插件目录
    detected: list[Path] = []

    # 检测 1: 当前工作目录下的 builtin/plugins
    for name in ["builtin", "plugins"]:
        p = Path(name).resolve()
        if p.exists():
            detected.append(p)

    # 检测 2: 包目录下的 builtin/plugins（如 nanobee/builtin）
    if not detected:
        package_dir = Path(__file__).parent.parent.resolve()  # nanobee/
        for name in ["builtin", "plugins"]:
            p = package_dir / name
            if p.exists():
                detected.append(p)

    # 检测 3: 使用默认值
    if not detected:
        detected = [Path("builtin").resolve(), Path("plugins").resolve()]

    return detected


def _discover_plugins(plugin_dirs: list[Path]) -> list[dict[str, Any]]:
    """扫描插件目录，发现所有已安装的插件。

    遍历每个插件目录，查找子目录中的 plugin.toml 文件，
    解析并返回插件元数据列表。

    Args:
        plugin_dirs: 插件目录路径列表

    Returns:
        插件信息列表，每项包含 name、version、description、type、enabled
    """
    from nanobee.kernel.plugin_manager import PluginDescriptor  # type: ignore

    plugins: list[dict[str, Any]] = []
    for plugin_dir in plugin_dirs:
        if not plugin_dir.exists():
            continue
        for sub_dir in plugin_dir.iterdir():
            if not sub_dir.is_dir() or sub_dir.name.startswith("_"):
                continue
            desc = PluginDescriptor.discover(sub_dir)
            if desc is None:
                continue
            plugins.append({
                "name": desc.metadata.name,
                "version": desc.metadata.version,
                "description": desc.metadata.description,
                "type": desc.metadata.plugin_type,
                "enabled": desc.config.get("config", {}).get("enabled", True),
                "path": str(desc.plugin_dir),
            })
    return plugins


def _format_plugin_table(plugins: list[dict[str, Any]]) -> str:
    """将插件列表格式化为表格字符串。

    Args:
        plugins: 插件信息列表

    Returns:
        格式化的表格字符串
    """
    if not plugins:
        return "未找到已安装的插件。"

    # 计算列宽
    name_width = max(len("NAME"), max(len(p["name"]) for p in plugins))
    version_width = max(len("VERSION"), max(len(p["version"]) for p in plugins))
    type_width = max(len("TYPE"), max(len(p["type"]) for p in plugins))

    # 构建表头
    header = (
        f"{'NAME':<{name_width}}  "
        f"{'VERSION':<{version_width}}  "
        f"{'TYPE':<{type_width}}  "
        f"{'STATUS':<8}  DESCRIPTION"
    )
    separator = "-" * len(header)

    lines = [separator, header, separator]

    for p in plugins:
        status = "enabled" if p["enabled"] else "disabled"
        line = (
            f"{p['name']:<{name_width}}  "
            f"{p['version']:<{version_width}}  "
            f"{p['type']:<{type_width}}  "
            f"{status:<8}  {p['description']}"
        )
        lines.append(line)

    lines.append(separator)
    lines.append(f"共 {len(plugins)} 个插件")
    return "\n".join(lines)


@click.group()
def plugin():
    """插件管理命令"""
    pass


@plugin.command("list")
@click.option(
    "-c", "--config",
    type=click.Path(exists=True, dir_okay=False, file_okay=True),
    help="配置文件路径 (YAML 格式)",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="以 JSON 格式输出",
)
def plugin_list(config: str | None, as_json: bool) -> None:
    """列出已安装的插件

    扫描配置中的插件目录（默认 builtin、plugins），
    显示所有已发现插件的元数据。
    """
    from pathlib import Path

    cfg = load_config(Path(config) if config else None)
    plugin_dirs = _resolve_plugin_dirs(cfg)
    plugins = _discover_plugins(plugin_dirs)

    if as_json:
        import json
        click.echo(json.dumps(plugins, indent=2, ensure_ascii=False))
    else:
        click.echo(_format_plugin_table(plugins))


@plugin.command()
@click.argument("name")
def create(name: str) -> None:
    """创建新插件

    在 plugins 目录下创建一个新插件目录，包含 plugin.toml 和 plugin.py 模板。

    Args:
        name: 插件名称（使用下划线分隔，如 my_tool）
    """
    import re

    # 验证插件名称（仅允许字母、数字、下划线）
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        click.echo("错误: 插件名称必须以字母开头，仅包含字母、数字和下划线", err=True)
        raise SystemExit(1)

    # 创建目录结构
    plugin_base = Path("plugins").resolve()
    plugin_dir = plugin_base / name

    if plugin_dir.exists():
        click.echo(f"错误: 插件目录已存在: {plugin_dir}", err=True)
        raise SystemExit(1)

    plugin_dir.mkdir(parents=True, exist_ok=True)

    # 创建 plugin.toml
    toml_content = f"""\
[plugin]
name = "{name}"
version = "1.0.0"
description = "{name} 插件"
author = ""
type = "tool"

[config]
enabled = true
"""
    (plugin_dir / "plugin.toml").write_text(toml_content, encoding="utf-8")

    # 创建 plugin.py 模板
    py_content = f"""\
\"\"\"{name} 插件\"\"\"

from __future__ import annotations

from typing import Any

from nanobee.plugins.tool import ToolPlugin


class {name.replace('_', ' ').title().replace(' ', '')}Plugin(ToolPlugin):
    \"\"\"{name} 工具插件\"\"\"

    name = "{name}"
    version = "1.0.0"

    def get_tools(self) -> list[dict[str, Any]]:
        \"\"\"获取工具定义列表\"\"\"
        return []

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        \"\"\"执行工具调用

        Args:
            tool_name: 工具名称
            **kwargs: 工具参数

        Returns:
            工具执行结果

        Raises:
            ValueError: 未知工具时抛出
        \"\"\"
        raise ValueError(f"未知工具: {{tool_name}}")
"""
    (plugin_dir / "plugin.py").write_text(py_content, encoding="utf-8")

    # 创建 __init__.py
    (plugin_dir / "__init__.py").write_text(f"\"\"\"{name} plugin.\"\"\"\n", encoding="utf-8")

    click.echo(f"插件已创建: {plugin_dir}")
    click.echo("  - plugin.toml  (插件配置)")
    click.echo("  - plugin.py    (插件实现)")
    click.echo("  - __init__.py  (模块标识)")


@plugin.command()
@click.argument("name")
@click.option(
    "-c", "--config",
    type=click.Path(exists=True, dir_okay=False, file_okay=True),
    help="配置文件路径 (YAML 格式)",
)
def enable(name: str, config: str | None) -> None:
    """启用插件

    在配置中启用指定插件。

    Args:
        name: 插件名称
    """
    click.echo(f"启用插件: {name}")
    click.echo("提示: 此功能需要与内核配合使用，当前为占位实现。")


@plugin.command()
@click.argument("name")
@click.option(
    "-c", "--config",
    type=click.Path(exists=True, dir_okay=False, file_okay=True),
    help="配置文件路径 (YAML 格式)",
)
def disable(name: str, config: str | None) -> None:
    """禁用插件

    在配置中禁用指定插件。

    Args:
        name: 插件名称
    """
    click.echo(f"禁用插件: {name}")
    click.echo("提示: 此功能需要与内核配合使用，当前为占位实现。")


def register(cli_group):
    """注册到主 CLI"""
    cli_group.add_command(plugin)
