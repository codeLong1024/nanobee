"""插件目录解析（纯函数，零副作用）"""

from __future__ import annotations

from pathlib import Path


def resolve_plugin_dirs(
    *,
    data_dir: Path,
    package_builtin: str,
    plugin_dirs: list[str] | None = None,
    config_dirs: list[str] | None = None,
) -> list[str]:
    """解析最终插件目录列表。

    优先级（从高到低）：
    1. 构造函数显式指定 (plugin_dirs)
    2. 配置文件指定 (config_dirs)
    3. 默认自动发现 <data_dir>/plugins/

    内置插件 (package_builtin) 始终在最前，除非显式 __replace__。

    Args:
        data_dir: 数据目录，用于解析相对路径和默认自动发现
        package_builtin: 内置插件包路径（如 nanobee/builtin/）
        plugin_dirs: 构造函数显式指定的插件目录
        config_dirs: 配置文件指定的插件目录

    Returns:
        有序的插件目录路径列表
    """
    use_builtin = True
    instance_dirs: list[str] = []

    if plugin_dirs is not None:
        use_builtin, instance_dirs = _parse_dirs(plugin_dirs, data_dir)
    elif config_dirs:
        use_builtin, instance_dirs = _parse_dirs(config_dirs, data_dir)
    else:
        default_dir = data_dir / "plugins"
        if default_dir.is_dir():
            instance_dirs = [str(default_dir)]

    if use_builtin:
        return [package_builtin] + instance_dirs
    return instance_dirs


def _parse_dirs(dirs: list[str], data_dir: Path) -> tuple[bool, list[str]]:
    """解析目录列表，处理 __replace__ 和空列表语义。

    Args:
        dirs: 原始目录列表
        data_dir: 数据目录，用于解析相对路径

    Returns:
        (use_builtin, resolved_dirs): use_builtin 是否保留内置插件，resolved_dirs 解析后的绝对路径列表
    """
    if not dirs:
        return False, []  # 显式空列表：不加载任何插件
    if dirs[0] == "__replace__":
        return False, [_resolve(data_dir, d) for d in dirs[1:]]
    return True, [_resolve(data_dir, d) for d in dirs]


def _resolve(data_dir: Path, d: str) -> str:
    """解析单个插件目录路径：相对路径基于 data_dir，绝对路径保持不变。"""
    p = Path(d)
    return str(p) if p.is_absolute() else str(data_dir / p)
