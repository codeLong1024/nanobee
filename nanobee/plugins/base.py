"""
NanobeePlugin 基类 - 所有插件的基类
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .hook_mixin import PluginHookMixin

logger = logging.getLogger(__name__)


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
        self.kernel: Any | None = None
        self._enabled = False

    @property
    def metadata(self) -> PluginMetadata:
        """获取插件元数据"""
        return self._metadata

    # ---- 生命周期方法 ----

    def initialize(self, kernel: Any) -> None:
        """初始化插件（由 PluginManager 调用）

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        logger.info("插件 %s 初始化完成", self._metadata.name)

    def on_load(self) -> None:
        """插件加载后调用（注册工具、注册事件等）"""
        pass

    def on_enable(self) -> None:
        """插件启用时调用"""
        self._enabled = True
        logger.info("插件 %s 已启用", self._metadata.name)

    def on_disable(self) -> None:
        """插件禁用时调用"""
        self._enabled = False
        logger.info("插件 %s 已禁用", self._metadata.name)

    def on_unload(self) -> None:
        """插件卸载前调用（清理资源）"""
        self.kernel = None

    def destroy(self) -> None:
        """销毁插件（由 PluginManager 调用）"""
        self.on_unload()
        logger.info("插件 %s 已销毁", self._metadata.name)

    # ---- 工具方法 ----

    @property
    def is_enabled(self) -> bool:
        """插件是否已启用"""
        return self._enabled

    def get_config(self, key: str, default: Any = None) -> Any:
        """从内核配置中获取当前插件的配置

        Args:
            key: 配置键名
            default: 默认值

        Returns:
            配置值
        """
        if self.kernel is None:
            return default
        plugin_config = self.kernel.config.get("plugins", {}).get(self._metadata.name, {})
        return plugin_config.get(key, default)

    def install(self) -> None:
        """安装插件（可选，例如创建必要的目录或文件）"""
        pass

    def uninstall(self) -> None:
        """卸载插件（清理安装时创建的内容）"""
        pass
