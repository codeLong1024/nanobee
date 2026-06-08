"""
插件管理器 - 管理所有插件的生命周期
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Type

import toml

from nanobee.plugins.base import NanobeePlugin, PluginMetadata

from nanobee.utils.logger import logger


class PluginDescriptor:
    """插件描述符，从 plugin.toml 解析"""

    def __init__(self, toml_path: Path):
        """从 plugin.toml 文件解析

        Args:
            toml_path: plugin.toml 文件路径
        """
        self.toml_path = toml_path
        self.plugin_dir = toml_path.parent

        with open(toml_path, "r", encoding="utf-8") as f:
            self._data = toml.load(f)

        plugin_section = self._data.get("plugin", {})
        # 支持 "type" 和 "plugin_type" 两种字段名（向后兼容）
        plugin_type = plugin_section.get("type") or plugin_section.get("plugin_type", "unknown")
        self.metadata = PluginMetadata(
            name=plugin_section.get("name", ""),
            version=plugin_section.get("version", "0.0.1"),
            description=plugin_section.get("description", ""),
            author=plugin_section.get("author", ""),
            plugin_type=plugin_type,
            dependencies=plugin_section.get("dependencies", {}).get("requires", []),
            permissions=self._parse_permissions(plugin_section),
        )
        self.config = self._data.get("config", {})

    def _parse_permissions(self, plugin_section: dict) -> list[str]:
        """解析权限声明"""
        perms = plugin_section.get("permissions", {})
        return [k for k, v in perms.items() if v is True]

    @property
    def main_module(self) -> Path | None:
        """查找插件主模块（plugin.py 或 __init__.py）"""
        for candidate in ["plugin.py", "__init__.py"]:
            path = self.plugin_dir / candidate
            if path.exists():
                return path
        return None

    @classmethod
    def discover(cls, plugin_dir: Path) -> PluginDescriptor | None:
        """从插件目录发现并解析 plugin.toml

        Args:
            plugin_dir: 插件目录

        Returns:
            PluginDescriptor 实例，如果不存在 plugin.toml 则返回 None
        """
        toml_path = plugin_dir / "plugin.toml"
        if not toml_path.exists():
            logger.warning("插件目录 {} 中未找到 plugin.toml", plugin_dir)
            return None
        return cls(toml_path)


class PluginManager:
    """插件管理器

    负责插件的扫描、加载、启用、禁用、卸载。
    """

    def __init__(self, kernel: Any, plugin_dirs: list[str] | None = None):
        """初始化插件管理器

        Args:
            kernel: NanobeeKernel 实例
            plugin_dirs: 插件目录列表，相对于工作目录
        """
        self.kernel = kernel
        self.plugin_dirs = [Path(d) for d in (plugin_dirs or ["builtin", "plugins"])]
        self._plugins: dict[str, NanobeePlugin] = {}  # name → instance
        self._descriptors: dict[str, PluginDescriptor] = {}  # name → descriptor

    # ---- 插件扫描 ----

    def scan(self) -> list[PluginDescriptor]:
        """扫描所有插件目录，发现可用插件

        Returns:
            PluginDescriptor 列表
        """
        descriptors = []
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                logger.warning("插件目录不存在: {}", plugin_dir)
                continue
            for sub_dir in plugin_dir.iterdir():
                if not sub_dir.is_dir() or sub_dir.name.startswith("_"):
                    continue
                desc = PluginDescriptor.discover(sub_dir)
                if desc:
                    descriptors.append(desc)
                    self._descriptors[desc.metadata.name] = desc
        return descriptors

    # ---- 插件加载 ----

    def load_plugin(self, descriptor: PluginDescriptor) -> NanobeePlugin | None:
        """加载单个插件

        Args:
            descriptor: 插件描述符

        Returns:
            加载成功的插件实例，失败返回 None
        """
        name = descriptor.metadata.name

        # 检查依赖
        for dep in descriptor.metadata.dependencies:
            if dep not in self._plugins:
                logger.error("插件 {} 缺少依赖: {}", name, dep)
                return None

        # 动态导入插件模块
        main_module = descriptor.main_module
        if main_module is None:
            logger.error("插件 {} 缺少主模块（plugin.py 或 __init__.py）", name)
            return None

        try:
            # 从插件目录路径派生出唯一的模块名，避免硬编码
            relative_plugin_dir = descriptor.plugin_dir.resolve()
            sanitized = "_".join(relative_plugin_dir.parts[-3:]) if len(relative_plugin_dir.parts) >= 3 \
                        else "_".join(relative_plugin_dir.parts)
            module_name = f"_nanobee_plugins.{sanitized}.{name}"
            spec = importlib.util.spec_from_file_location(module_name, main_module)
            if spec is None or spec.loader is None:
                logger.error("插件 {} 的文件无法加载为 Python 模块: {}", name, main_module)
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            # 查找插件类（继承 NanobeePlugin 的类）
            plugin_class = self._find_plugin_class(module, descriptor.metadata.plugin_type)
            if plugin_class is None:
                logger.error("插件 {} 中未找到有效的插件类", name)
                return None

            # 实例化插件
            plugin_instance = plugin_class(metadata=descriptor.metadata)
            plugin_instance.initialize(self.kernel)
            plugin_instance.on_load()

            self._plugins[name] = plugin_instance
            logger.info("插件 {} 加载成功", name)
            return plugin_instance

        except Exception as e:
            logger.exception(f"加载插件 {name} 失败: {e}")
            return None

    def _find_plugin_class(self, module: Any, plugin_type: str) -> Type[NanobeePlugin] | None:
        """在模块中查找具体的插件类，排除导入的抽象基类。

        策略：跳过有空 __abstractmethods__ 的类（抽象基类），
        因为 Python 的 ABCMeta 会在基类上设置 __abstractmethods__，
        而具体子类实现所有抽象方法后该集合为空。

        Args:
            module: 动态加载的插件模块
            plugin_type: 插件类型（用于日志，暂未使用）

        Returns:
            插件类，未找到返回 None
        """
        candidates: list[type[NanobeePlugin]] = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, NanobeePlugin)
                and attr is not NanobeePlugin
                # 跳过抽象基类（有未实现的抽象方法）
                and not getattr(attr, "__abstractmethods__", None)
            ):
                candidates.append(attr)

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # 多个候选时取最深继承层级（最具体子类）
        candidates.sort(key=lambda cls: len(cls.__mro__), reverse=True)
        return candidates[0]

    def load_all(self) -> None:
        """扫描并加载所有插件"""
        descriptors = self.scan()
        # 按依赖顺序加载（拓扑排序）
        loaded = set()
        for desc in descriptors:
            self._load_with_deps(desc, loaded)

    def _load_with_deps(self, desc: PluginDescriptor, loaded: set) -> None:
        """递归加载插件及其依赖"""
        name = desc.metadata.name
        if name in loaded or name in self._plugins:
            return
        for dep in desc.metadata.dependencies:
            dep_desc = self._descriptors.get(dep)
            if dep_desc:
                self._load_with_deps(dep_desc, loaded)
        self.load_plugin(desc)
        loaded.add(name)

    # ---- 插件查询 ----

    def get(self, name: str) -> NanobeePlugin | None:
        """获取插件实例"""
        return self._plugins.get(name)

    def get_by_type(self, plugin_type: str) -> list[NanobeePlugin]:
        """按类型获取已成功加载的插件列表，基于插件实例本身的元数据过滤。"""
        return [
            plugin
            for plugin in self._plugins.values()
            if plugin.metadata.plugin_type == plugin_type
        ]

    def get_descriptor(self, name: str) -> PluginDescriptor | None:
        """获取插件描述符。

        Args:
            name: 插件名称

        Returns:
            PluginDescriptor 实例，未找到返回 None
        """
        return self._descriptors.get(name)

    def list_plugins(self) -> list[str]:
        """列出所有已加载的插件名称"""
        return list(self._plugins.keys())

    def is_enabled(self, name: str) -> bool:
        """检查插件是否已启用。

        Args:
            name: 插件名称

        Returns:
            bool: 插件是否已启用
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        return plugin.is_enabled

    def get_enabled_plugins(self) -> list[NanobeePlugin]:
        """获取所有已启用的插件。"""
        return [p for p in self._plugins.values() if p.is_enabled]

    # ---- 插件生命周期 ----

    def _set_enabled(self, name: str, state: bool, callback: str) -> bool:
        """设置插件启用状态（内部方法）。

        Args:
            name: 插件名称
            state: True=启用，False=禁用
            callback: 回调方法名（"on_enable" 或 "on_disable"）

        Returns:
            bool: 插件是否存在且操作成功返回 True，插件未加载返回 False
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            logger.error("插件 {} 未加载", name)
            return False
        method = getattr(plugin, callback, None)
        if method is not None and callable(method):
            method()
        return True

    def enable(self, name: str) -> bool:
        """启用插件"""
        return self._set_enabled(name, state=True, callback="on_enable")

    def disable(self, name: str) -> bool:
        """禁用插件"""
        return self._set_enabled(name, state=False, callback="on_disable")

    def unload(self, name: str) -> bool:
        """卸载插件"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        plugin.destroy()
        del self._plugins[name]
        logger.info("插件 {} 已卸载", name)
        return True

    def unload_all(self) -> None:
        """卸载所有插件"""
        for name in list(self._plugins.keys()):
            self.unload(name)
