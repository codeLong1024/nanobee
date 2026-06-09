"""模型预设管理器 — 管理模型预设解析和 provider 快照状态。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nanobee.agent.model_presets import PresetSnapshotLoader
from nanobee.config.schema import ModelPresetConfig
from nanobee.providers.base import LLMProvider
from nanobee.providers.factory import ProviderSnapshot
from nanobee.utils.logger import logger


class ModelPresetManager:
    """模型预设管理器。

    管理模型预设的解析、provider 快照的轮询和签名跟踪。
    不持有运行时状态（provider/model 等），只管理预设元数据。

    职责：
    - 持有预设配置字典和加载器
    - 活动预设名称跟踪
    - provider 快照签名跟踪（防重复应用）
    - 预设名称验证和规范化
    - ProviderSnapshot 构建
    """

    def __init__(
        self,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        preset_snapshot_loader: PresetSnapshotLoader | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
    ) -> None:
        """初始化预设管理器。

        Args:
            model_presets: 模型预设配置字典
            preset_snapshot_loader: 预设快照加载器（可选的延迟加载）
            provider_snapshot_loader: provider 快照加载器（用于运行时切换）
        """
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._preset_snapshot_loader = preset_snapshot_loader
        self._provider_snapshot_loader = provider_snapshot_loader
        self._active_preset: str | None = None
        self._provider_signature: tuple | None = None

    @property
    def active_preset(self) -> str | None:
        """当前激活的预设名称。"""
        return self._active_preset

    def set_active(self, name: str | None) -> None:
        """设置活动预设名称。"""
        self._active_preset = name

    def normalize_name(self, name: str) -> str:
        """验证并规范化预设名称。

        Args:
            name: 预设名称

        Returns:
            规范化后的预设名称

        Raises:
            ValueError: 名称为空
            KeyError: 名称不在预设字典中
        """
        from nanobee.agent.model_presets import normalize_preset_name

        return normalize_preset_name(name, self.model_presets)

    def build_snapshot(self, name: str, provider: LLMProvider) -> ProviderSnapshot:
        """为指定预设名称构建 ProviderSnapshot。

        Args:
            name: 已规范化的预设名称
            provider: 当前 LLM Provider 实例

        Returns:
            ProviderSnapshot 实例
        """
        from nanobee.agent.model_presets import build_runtime_preset_snapshot

        return build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=provider,
            loader=self._preset_snapshot_loader,
        )

    def check_and_get_snapshot(self) -> ProviderSnapshot | None:
        """轮询外部配置源，返回新快照或 None。

        如果加载器未配置、加载失败或签名未变，返回 None。

        Returns:
            新的 ProviderSnapshot，或 None 表示无变化
        """
        if self._provider_snapshot_loader is None:
            return None
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("刷新 provider 配置失败")
            return None
        if snapshot.signature == self._provider_signature:
            return None
        return snapshot

    def record_applied_snapshot(self, snapshot: ProviderSnapshot) -> None:
        """记录已应用的快照签名，防止重复应用。

        Args:
            snapshot: 已应用的 ProviderSnapshot
        """
        self._provider_signature = snapshot.signature

    def has_presets(self) -> bool:
        """是否有任何预设配置。"""
        return bool(self.model_presets)
