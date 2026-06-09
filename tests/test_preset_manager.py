"""测试 ModelPresetManager — 模型预设管理器。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nanobee.agent.preset_manager import ModelPresetManager
from nanobee.config.schema import ModelPresetConfig
from nanobee.providers.base import LLMProvider
from nanobee.providers.factory import ProviderSnapshot


class _DummyProvider(LLMProvider):
    """测试用假 Provider。"""

    def __init__(self):
        self.model = "gpt-4"
        self.generation = None
        self._default_model = "gpt-4"

    def get_default_model(self) -> str:
        return self._default_model

    async def chat(self, **kwargs):
        pass

    async def chat_with_retry(self, **kwargs):
        pass

    async def chat_stream_with_retry(self, **kwargs):
        pass


class TestModelPresetManagerInit:
    """ModelPresetManager 初始化测试。"""

    def test_default_init(self):
        """默认初始化。"""
        mgr = ModelPresetManager()
        assert mgr.active_preset is None
        assert mgr.model_presets == {}
        assert not mgr.has_presets()

    def test_with_presets(self):
        """带预设配置初始化。"""
        presets = {
            "fast": ModelPresetConfig(model="gpt-3.5-turbo"),
            "strong": ModelPresetConfig(model="gpt-4"),
        }
        mgr = ModelPresetManager(model_presets=presets)
        assert mgr.has_presets()
        assert mgr.active_preset is None


class TestModelPresetManagerActive:
    """活动预设管理测试。"""

    def test_set_active(self):
        """设置活动预设。"""
        mgr = ModelPresetManager()
        mgr.set_active("fast")
        assert mgr.active_preset == "fast"

    def test_set_active_none(self):
        """清除活动预设。"""
        mgr = ModelPresetManager()
        mgr.set_active("fast")
        mgr.set_active(None)
        assert mgr.active_preset is None


class TestModelPresetManagerNormalize:
    """预设名称规范化测试。"""

    def setup_method(self):
        presets = {
            "fast": ModelPresetConfig(model="gpt-3.5-turbo"),
            "strong": ModelPresetConfig(model="gpt-4"),
        }
        self.mgr = ModelPresetManager(model_presets=presets)

    def test_normalize_valid(self):
        """有效预设名称返回自身。"""
        assert self.mgr.normalize_name("fast") == "fast"

    def test_normalize_empty_raises(self):
        """空名称抛出 ValueError。"""
        with pytest.raises(ValueError):
            self.mgr.normalize_name("")

    def test_normalize_missing_raises(self):
        """不存在的名称抛出 KeyError。"""
        with pytest.raises(KeyError):
            self.mgr.normalize_name("nonexistent")


class TestModelPresetManagerSnapshot:
    """Provider 快照管理测试。"""

    def setup_method(self):
        presets = {
            "fast": ModelPresetConfig(model="gpt-3.5-turbo"),
        }
        self.mgr = ModelPresetManager(model_presets=presets)
        self.provider = _DummyProvider()

    def test_build_snapshot(self):
        """构建 ProviderSnapshot。"""
        snapshot = self.mgr.build_snapshot("fast", self.provider)
        assert snapshot is not None
        assert snapshot.model == "gpt-3.5-turbo"

    def test_check_no_loader(self):
        """无 provider_snapshot_loader 时返回 None。"""
        assert self.mgr.check_and_get_snapshot() is None

    def test_check_same_signature(self):
        """签名未变更时返回 None。"""
        snapshot = ProviderSnapshot(
            provider=self.provider,
            model="gpt-4",
            context_window_tokens=8192,
            signature=("test", "v1"),
        )
        self.mgr.record_applied_snapshot(snapshot)
        loader = lambda: ProviderSnapshot(
            provider=self.provider,
            model="gpt-4",
            context_window_tokens=8192,
            signature=("test", "v1"),
        )
        self.mgr._provider_snapshot_loader = loader
        assert self.mgr.check_and_get_snapshot() is None

    def test_check_new_signature(self):
        """签名变更时返回新快照。"""
        snapshot = ProviderSnapshot(
            provider=self.provider,
            model="gpt-4",
            context_window_tokens=8192,
            signature=("test", "v1"),
        )
        self.mgr.record_applied_snapshot(snapshot)
        new_snapshot = ProviderSnapshot(
            provider=self.provider,
            model="gpt-4",
            context_window_tokens=8192,
            signature=("test", "v2"),
        )
        loader = lambda: new_snapshot
        self.mgr._provider_snapshot_loader = loader
        result = self.mgr.check_and_get_snapshot()
        assert result is new_snapshot

    def test_loader_exception_returns_none(self):
        """加载器抛出异常时返回 None。"""
        self.mgr._provider_snapshot_loader = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        assert self.mgr.check_and_get_snapshot() is None

    def test_record_applied(self):
        """记录已应用的快照。"""
        snapshot = ProviderSnapshot(
            provider=self.provider,
            model="gpt-4",
            context_window_tokens=8192,
            signature=("test", "v1"),
        )
        self.mgr.record_applied_snapshot(snapshot)
        assert self.mgr._provider_signature == ("test", "v1")
