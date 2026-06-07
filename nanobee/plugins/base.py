"""
NanobeePlugin 基类 - 所有插件的基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .hook_mixin import PluginHookMixin

from nanobee.utils.logger import logger



class PluginMetadata(BaseModel):
    """插件元数据，从 plugin.toml 解析"""

    name: str = ""
    version: str = "0.0.1"
    description: str = ""
    author: str = ""
    plugin_type: str = "unknown"  # tool | channel | memory | skill | dream
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class NanobeePlugin(PluginHookMixin, ABC):
    """插件基类

    所有插件必须继承此类，并实现必要的生命周期方法。
    继承 PluginHookMixin 以获得 5 个生命周期 Hook 的默认实现。
    """

    # 类级元数据（可通过 plugin.toml 覆盖）
    name: ClassVar[str] = "base"
    version: ClassVar[str] = "0.0.1"
    plugin_type: ClassVar[str] = "unknown"

    def __init__(self, metadata: PluginMetadata | None = None):
        """初始化插件

        Args:
            metadata: 从 plugin.toml 解析的元数据，为 None 时使用类级默认值
        """
        self._metadata = metadata or PluginMetadata(
            name=self.name,
            version=self.version,
            plugin_type=self.plugin_type,
        )
        self._kernel: Any | None = None  # 私有属性，禁止插件直接访问
        self._enabled = False
        self._config: dict[str, Any] = {}  # 插件专属配置（隔离）
        self._tmp: Path | None = None  # 插件临时目录（框架注入）

    @property
    def metadata(self) -> PluginMetadata:
        """获取插件元数据"""
        return self._metadata

    @property
    def kernel(self) -> Any | None:
        """获取内核实例（只读，插件不应直接访问 config）"""
        return self._kernel

    # ---- 生命周期方法 ----

    def initialize(self, kernel: Any) -> None:
        """初始化插件（由 PluginManager 调用）

        Args:
            kernel: NanobeeKernel 实例
        """
        self._kernel = kernel
        self._extract_config()
        logger.info("插件 {} 初始化完成", self._metadata.name)

    def _extract_config(self) -> None:
        """从内核配置中提取当前插件的专属配置（配置隔离）。

        每个插件只能读取自己在 plugins.<plugin_name> 下的配置段，
        无法访问其他插件的配置或全局配置。
        """
        if self._kernel is None:
            self._config = {}
            return
        # 兼容 kernel 为 dict 的情况（测试场景）
        if isinstance(self._kernel, dict):
            self._config = {}
            return
        # kernel.config 可能是 Config 对象（有 .plugins）或普通 dict
        cfg = self._kernel.config
        if hasattr(cfg, "plugins"):
            plugins_cfg = cfg.plugins
        else:
            plugins_cfg = cfg.get("plugins", {})
        plugin_config = plugins_cfg.get(self._metadata.name, {}) if isinstance(plugins_cfg, dict) else {}
        self._config = dict(plugin_config) if isinstance(plugin_config, dict) else {}

    def on_load(self) -> None:
        """插件加载后调用（注册工具、注册事件等）"""
        pass

    def on_enable(self) -> None:
        """插件启用时调用"""
        self._enabled = True
        logger.info("插件 {} 已启用", self._metadata.name)

    def on_disable(self) -> None:
        """插件禁用时调用"""
        self._enabled = False
        logger.info("插件 {} 已禁用", self._metadata.name)

    def on_unload(self) -> None:
        """插件卸载前调用（清理资源）"""
        self._kernel = None
        self._config = {}

    def destroy(self) -> None:
        """销毁插件（由 PluginManager 调用）"""
        self.on_unload()
        logger.info("插件 {} 已销毁", self._metadata.name)

    # ---- 工具方法 ----

    @property
    def tmp(self) -> Path | None:
        """插件临时目录（框架通过 ContextVar 按请求注入）

        路径：<context_root>/../tmp/<plugin_name>/
        框架只创建目录，清理由插件自己决定。
        未绑定 ContextVar 时返回 None（例如 boot 阶段或测试环境）。
        """
        from nanobee.kernel.context_sandbox_var import current_tmp
        _tmp_base = current_tmp()
        if _tmp_base is None:
            return None
        plugin_tmp = _tmp_base / self._metadata.name
        plugin_tmp.mkdir(parents=True, exist_ok=True)
        return plugin_tmp

    @property
    def is_enabled(self) -> bool:
        """插件是否已启用"""
        return self._enabled

    def get_config(self, key: str, default: Any = None) -> Any:
        """从插件专属配置中获取指定键的值

        每个插件只能访问自己在 plugins.<plugin_name> 下的配置段，
        无法读取其他插件的配置或全局配置。

        Args:
            key: 配置键名
            default: 默认值

        Returns:
            配置值
        """
        return self._config.get(key, default)

    def install(self) -> None:
        """安装插件（可选，例如创建必要的目录或文件）"""
        pass

    def uninstall(self) -> None:
        """卸载插件（清理安装时创建的内容）"""
        pass
