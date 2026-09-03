"""
NanobeePlugin 基类 - 所有插件的基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .hook_mixin import PluginHookMixin

from nanobee.utils.logger import logger


class HookConfig(BaseModel):
    """单个 Hook 的调度元数据。

    插件通过此声明告知框架如何调度该 Hook，框架只读标记、不懂含义。
    这是 FIP 合规的核心：策略由插件声明，机制由框架执行。

    Attributes:
        block_next: 是否阻塞同 context_id 的下一条消息的 dispatch
        priority: 同 block_next 组内的执行优先级，数值越大越优先
        timeout: 阻塞型 Hook 的超时时间（秒），0 表示不设超时
    """

    block_next: bool = False
    priority: int = 10
    timeout: float = 0.0


class PluginMetadata(BaseModel):
    """插件元数据，从 plugin.toml 解析"""

    name: str = ""
    version: str = "0.0.1"
    description: str = ""
    author: str = ""
    plugin_type: str = "unknown"  # tool | channel | memory | skill | dream
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    throttle_group: str = ""  # 节流分组标识，同组工具共享节流计数
    exec_capable: bool = False  # 是否具备命令执行能力（用于工作区逃逸检测）
    file_edit_capability: bool = False  # 是否具备文件编辑能力（用于进度追踪）
    hooks: dict[str, HookConfig] = Field(default_factory=dict)

    @field_validator("hooks", mode="before")
    @classmethod
    def _coerce_hooks(cls, v: Any) -> dict[str, HookConfig]:
        """将 hooks 字典中的 dict 值自动转换为 HookConfig 对象。

        非法类型（非 dict / 非 HookConfig）记录 warning 后降级为默认值，
        不抛异常——一个插件的配置错误不应阻塞框架启动。
        """
        if not isinstance(v, dict):
            return {}
        result: dict[str, HookConfig] = {}
        for key, val in v.items():
            if isinstance(val, HookConfig):
                result[key] = val
            elif isinstance(val, dict):
                result[key] = HookConfig(**val)
            else:
                logger.warning(
                    "hooks.{} 的值类型非法 ({}), 期望 dict 或 HookConfig, 已降级使用默认值",
                    key, type(val).__name__,
                )
                result[key] = HookConfig()
        return result


class NanobeePlugin(PluginHookMixin, ABC):
    """插件基类

    所有插件必须继承此类，并实现必要的生命周期方法。
    继承 PluginHookMixin 以获得 5 个生命周期 Hook 的默认实现。

    元数据由 PluginManager 从 plugin.toml 解析后强制传入，
    不存在类级兜底——plugin.toml 是唯一真实源。
    """

    # 插件声明式配置模型（pydantic BaseModel 子类）。
    # None = 未声明，_config 保持 dict 原样透传（向后兼容旧插件）。
    # FIP：框架只读标记、机械执行 model_validate，不懂字段含义。
    config_cls: type[BaseModel] | None = None

    def __init__(self, metadata: PluginMetadata):
        """初始化插件

        Args:
            metadata: 从 plugin.toml 解析的元数据，必传。测试中显式构造 PluginMetadata。
        """
        self._metadata = metadata
        self._kernel: Any | None = None  # 私有属性，禁止插件直接访问
        self._enabled = False
        # 声明 config_cls 时初始化默认实例（字段默认值可用），initialize() 再
        # 用实际配置覆盖；未声明时保持空 dict（向后兼容旧插件）。
        self._config: Any = self.config_cls() if self.config_cls is not None else {}
        self._tmp: Path | None = None  # 插件临时目录（框架注入）

    @property
    def metadata(self) -> PluginMetadata:
        """获取插件元数据"""
        return self._metadata

    @property
    def name(self) -> str:
        """插件名称（委托 metadata.name）"""
        return self._metadata.name

    @property
    def version(self) -> str:
        """插件版本（委托 metadata.version）"""
        return self._metadata.version

    @property
    def plugin_type(self) -> str:
        """插件类型（委托 metadata.plugin_type）"""
        return self._metadata.plugin_type

    @property
    def hook_config(self) -> dict[str, "HookConfig"]:
        """插件声明的 Hook 调度元数据。

        从 metadata.hooks 中提取，返回 {hook_name: HookConfig}。
        FIP 原则：框架只读标记，不懂含义。插件通过此元数据
        自行决定各 Hook 的调度策略（阻塞、优先级等）。

        插件未声明时为 {}。
        """
        return self._metadata.hooks

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

        声明了 ``config_cls`` 的插件，框架在此统一 ``model_validate``，
        完成类型强转（``"false"`` → ``False``、``"8080"`` → ``8080``）、
        约束校验、默认值填充与非法值降级；未声明的插件保持 dict 原样透传。
        """
        raw = self._read_plugins_section()
        if self.config_cls is None:
            self._config = raw
            return
        self._config = self._coerce_config(raw)

    def _coerce_config(self, raw: dict[str, Any]) -> BaseModel:
        """字段级降级校验配置：非法字段回退默认值，合法字段保留。

        逐次剔除 ``ValidationError`` 定位的非法字段并重试，避免一个字段
        错误连带丢弃其余所有合法配置；若无法进一步剔除（如必填字段缺失），
        整体回退默认实例，防止死循环。

        Args:
            raw: ``plugins.<plugin_name>`` 段的原始配置 dict。

        Returns:
            校验通过的 config_cls 实例。
        """
        while True:
            try:
                return self.config_cls.model_validate(raw)
            except ValidationError as exc:
                bad = {err["loc"][0] for err in exc.errors() if err["loc"]}
                cleaned = {k: v for k, v in raw.items() if k not in bad}
                if cleaned == raw:
                    return self.config_cls()
                logger.warning(
                    "[plugin] {} 配置字段 {} 非法，已回退默认值: {}",
                    self._metadata.name, sorted(bad), exc,
                )
                raw = cleaned

    def _read_plugins_section(self) -> dict[str, Any]:
        """读取 plugins.<plugin_name> 段的原始配置 dict。

        兼容 kernel 为 None、dict（测试场景）或 Config 对象三种形态。

        Returns:
            原始配置 dict；无法提取时返回空 dict。
        """
        if self._kernel is None or isinstance(self._kernel, dict):
            return {}
        cfg = self._kernel.config
        if hasattr(cfg, "plugins"):
            plugins_cfg = cfg.plugins
        else:
            plugins_cfg = cfg.get("plugins", {})
        plugin_config = plugins_cfg.get(self._metadata.name, {}) if isinstance(plugins_cfg, dict) else {}
        return dict(plugin_config) if isinstance(plugin_config, dict) else {}

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
        self._config = self.config_cls() if self.config_cls is not None else {}

    def destroy(self) -> None:
        """销毁插件（由 PluginManager 调用）"""
        self.on_unload()
        logger.info("插件 {} 已销毁", self._metadata.name)

    # ---- 工具方法 ----

    @property
    def tmp(self) -> Path | None:
        """插件临时目录（框架通过 ContextVar 按请求注入）

        路径：<user_context>/tmp/<plugin_name>/
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
    def context_root(self) -> Path | None:
        """用户上下文根目录（框架通过 ContextVar 按请求注入）

        路径：<user_context>/
        框架只提供 basedir，插件拿到后自己创建所需的持久化子目录。
        未绑定 ContextVar 时返回 None（例如 boot 阶段或测试环境）。
        """
        from nanobee.kernel.context_sandbox_var import current_context_root
        return current_context_root()

    @property
    def is_enabled(self) -> bool:
        """插件是否已启用"""
        return self._enabled

    @property
    def config(self) -> Any | None:
        """强类型配置对象。

        声明 ``config_cls`` 时返回 model 实例；未声明（dict 存储）时返回 None。
        """
        return None if isinstance(self._config, dict) else self._config

    def get_config(self, key: str, default: Any = None) -> Any:
        """从插件专属配置中获取指定键的值。

        兼容两种存储：未声明 config_cls 时为 dict（``.get`` 语义）；
        声明后为强类型 model（字段访问，键不存在时回退 default）。

        Args:
            key: 配置键名
            default: 默认值

        Returns:
            配置值
        """
        if isinstance(self._config, dict):
            return self._config.get(key, default)
        if key in type(self._config).model_fields:
            return getattr(self._config, key)
        return default

    def install(self) -> None:
        """安装插件（可选，例如创建必要的目录或文件）"""
        pass

    def uninstall(self) -> None:
        """卸载插件（清理安装时创建的内容）"""
        pass
