"""Configuration loader (MVP 最小化版本).
从 YAML 文件加载配置并解析环境变量占位符。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from nanobee.config.schema import Config, ModelPresetConfig, AgentProviderConfig

_ENV_VAR_RE = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def resolve_config_env_vars(config: Config) -> Config:
    """递归解析配置中所有 ${VAR_NAME} 和 ${VAR_NAME:-default} 占位符。

    会就地修改配置对象并返回。
    """
    def _resolve(obj: Any) -> Any:
        if isinstance(obj, str):
            def _replace(m: re.Match) -> str:
                var_name = m.group(1)
                default = m.group(2)
                return os.environ.get(var_name, default) if default else os.environ.get(var_name, m.group(0))
            return _ENV_VAR_RE.sub(_replace, obj)
        if isinstance(obj, dict):
            return {k: _resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve(v) for v in obj]
        return obj

    raw = _resolve(config.model_dump())
    return Config(**raw)


def load_config(config_path: Path | None = None) -> Config:
    """从 YAML 文件加载配置。

    Args:
        config_path: YAML 配置文件路径。为 None 时返回全默认 Config。

    Returns:
        解析后的 Config 对象。
    """
    if config_path is None:
        return Config()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping, got {type(raw).__name__}")

    # 将 providers 展开为 AgentProviderConfig
    providers_raw = raw.pop("providers", {}) or {}
    providers = {
        name: AgentProviderConfig(**provider_data)
        for name, provider_data in providers_raw.items()
    }

    # 将 model_presets 展开
    presets_raw = raw.pop("model_presets", {}) or {}
    presets = {
        name: ModelPresetConfig(**preset_data)
        for name, preset_data in presets_raw.items()
    }

    config = Config(**raw)
    config.providers = providers
    config.model_presets = presets
    return config
