"""Configuration schema using Pydantic."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    """基类，同时接受 camelCase 和 snake_case 键名。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class InlineFallbackConfig(Base):
    """单个内联降级模型配置。"""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


FallbackCandidate = str | InlineFallbackConfig


class GenerationSettings(Base):
    """生成参数设置（用于 ModelPresetConfig 的方法返回）。"""

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


class ModelPresetConfig(Base):
    """命名模型预设，用于快速切换模型 + 生成参数。"""

    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    reasoning_effort: str | None = None

    def to_generation_settings(self) -> GenerationSettings:
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentProviderConfig(Base):
    """Provider 级别配置项。"""

    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    region: str | None = None
    profile: str | None = None


class AgentDefaults(Base):
    """默认 Agent 配置。"""

    model_preset: str | None = None
    model: str = "anthropic/claude-sonnet-4-20250514"
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    fallback_models: list[FallbackCandidate] = []
    max_messages: int = 120
    memory_store_threshold: int = 20
    max_iterations: int = 10
    max_concurrent_subagents: int = 4
    blacklist: list[str] = []
    whitelist: list[str] = []


class AgentsConfig(Base):
    """Agent 组配置。"""

    defaults: AgentDefaults = AgentDefaults()


class LoggingConfig(Base):
    """运行时日志配置（程序自管理，loguru 文件 sink）。"""

    dir: str = "logs"
    file: str | None = None
    level: str = "INFO"
    rotation: str = "500 MB"
    retention: str = "30 days"
    compression: str = "gz"
    json_format: bool = False


class GatewayConfig(Base):
    """网关配置（涵盖端口与多实例运行时管理）。

    所有阈值均可通过 nanobee.yaml 覆盖，零硬编码。
    pid_dir 为空时自动推导为 <data_dir>/.pid/。
    """

    port: int = 8080
    stop_timeout: float = 20.0
    health_check_timeout: float = 10.0
    health_check_interval: float = 1.0
    pid_dir: str = ""
    restart_delay: float = 2.0


class ToolsConfig(Base):
    """工具安全配置。"""

    ssrf_whitelist: list[str] = []


class Config(BaseModel):
    """nanobee 顶层配置对象。

    提供 factory.py 和 model_presets.py 所需的所有方法签名。
    实际使用时应从 YAML 文件加载。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data_dir: str = "~/.nanobee"
    core_md_path: str = "core.md"
    gateway: GatewayConfig = GatewayConfig()
    plugin_dirs: list[str] = []
    model_presets: dict[str, ModelPresetConfig] = {}
    agents: AgentsConfig = AgentsConfig()
    providers: dict[str, AgentProviderConfig] = {}
    mcp_servers: dict[str, dict[str, Any]] = {}
    routing: dict[str, str] = {}
    channels: dict[str, dict[str, Any]] = {}
    plugins: dict[str, dict[str, Any]] = {}
    logging: LoggingConfig = LoggingConfig()
    tools: ToolsConfig = ToolsConfig()

    def resolve_preset(self, name: str | None) -> ModelPresetConfig:
        """按名称解析模型预设，返回 None 时使用默认预设。"""
        if name is not None and name in self.model_presets:
            return self.model_presets[name]
        return self.resolve_default_preset()

    def resolve_default_preset(self) -> ModelPresetConfig:
        """返回默认模型预设。"""
        defaults = self.agents.defaults
        preset_name = defaults.model_preset
        if preset_name and preset_name in self.model_presets:
            return self.model_presets[preset_name]
        return ModelPresetConfig(
            model=defaults.model,
            provider=defaults.provider,
            max_tokens=defaults.max_tokens,
            context_window_tokens=defaults.context_window_tokens,
            temperature=defaults.temperature,
        )

    def get_provider(self, model: str, preset: ModelPresetConfig | None = None) -> AgentProviderConfig | None:
        """获取 provider 配置。"""
        provider_name = preset.provider if preset else "auto"
        return self.providers.get(provider_name)

    def get_provider_name(self, model: str, preset: ModelPresetConfig | None = None) -> str:
        """获取 provider 名称。"""
        return (preset.provider if preset else "auto") or "auto"

    def get_api_key(self, model: str, preset: ModelPresetConfig | None = None) -> str | None:
        provider = self.get_provider(model, preset)
        return provider.api_key if provider else None

    def get_api_base(self, model: str, preset: ModelPresetConfig | None = None) -> str | None:
        provider = self.get_provider(model, preset)
        return provider.api_base if provider else None
