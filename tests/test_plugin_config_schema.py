"""config_cls 声明式配置 schema 机制测试。

覆盖：类型强转、约束校验、非法值降级、默认值填充、dict 向后兼容、
config property 与 get_config 的双形态兼容。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nanobee.plugins.base import NanobeePlugin, PluginMetadata


class _DemoConfig(BaseModel):
    """测试用声明式配置模型。"""

    enabled: bool = False
    port: int = Field(default=8080, ge=1, le=65535)
    host: str = "127.0.0.1"


class _DemoPlugin(NanobeePlugin):
    """声明 config_cls 的测试插件。"""

    config_cls = _DemoConfig


class _LegacyPlugin(NanobeePlugin):
    """未声明 config_cls 的测试插件（向后兼容路径）。"""


class _Cfg:
    """模拟 kernel.config（带 plugins 属性的简单对象）。"""

    def __init__(self, plugins: dict) -> None:
        self.plugins = plugins


class _Kernel:
    """模拟 NanobeeKernel（仅供 initialize 配置提取）。"""

    def __init__(self, plugins: dict | None = None) -> None:
        self.config = _Cfg(plugins or {})


def _make_plugin(cls: type[NanobeePlugin], name: str = "demo") -> NanobeePlugin:
    return cls(PluginMetadata(name=name, plugin_type="tool"))


class TestConfigClsMechanism:
    """config_cls 声明时的强转 / 校验 / 降级。"""

    def test_coerces_string_types(self):
        """字符串 "false"/"8080" 被框架强转为 bool/int。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {"enabled": "false", "port": "8080"}}))

        assert plugin.config.enabled is False
        assert plugin.config.port == 8080
        assert plugin.config.host == "127.0.0.1"

    def test_applies_defaults(self):
        """空配置段填充为 config_cls 的默认值。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {}}))

        assert plugin.config.enabled is False
        assert plugin.config.port == 8080

    def test_invalid_range_falls_back_to_default(self):
        """越界值（ge/le 约束）字段回退默认值，且不抛异常。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {"port": 99999}}))

        assert plugin.config.port == 8080

    def test_invalid_type_falls_back_to_default(self):
        """类型非法（"abc" 给 int 字段）字段回退默认值，且不抛异常。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {"port": "abc"}}))

        assert plugin.config.port == 8080

    def test_invalid_field_keeps_valid_fields(self):
        """字段级降级：一个字段非法时，其余合法字段保留，不被连带丢弃。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(
            _Kernel({"demo": {"enabled": True, "host": "0.0.0.0", "port": "abc"}})
        )

        assert plugin.config.enabled is True
        assert plugin.config.host == "0.0.0.0"
        assert plugin.config.port == 8080

    def test_all_fields_invalid_falls_back_to_defaults(self):
        """全部字段非法时整体回退默认实例，且不抛异常。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(
            _Kernel({"demo": {"enabled": "not-bool", "port": "abc", "host": 123}})
        )

        assert plugin.config.enabled is False
        assert plugin.config.port == 8080
        assert plugin.config.host == "127.0.0.1"

    def test_unload_keeps_model_defaults(self):
        """声明 config_cls 的插件 unload 后 _config 重置为默认 model 实例（非 dict）。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {"enabled": True}}))
        plugin.on_unload()

        assert isinstance(plugin.config, _DemoConfig)
        assert plugin.config.enabled is False
        assert plugin.get_config("port") == 8080

    def test_kernel_none_yields_defaults(self):
        """kernel 为 None 时回退 config_cls 默认实例。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(None)

        assert isinstance(plugin.config, _DemoConfig)
        assert plugin.config.port == 8080


class TestBackwardCompat:
    """未声明 config_cls 的插件保持 dict 原样透传。"""

    def test_legacy_config_is_dict(self):
        """未声明 config_cls 时 _config 为 dict，config property 为 None。"""
        plugin = _make_plugin(_LegacyPlugin, name="legacy")
        plugin.initialize(_Kernel({"legacy": {"key": "value"}}))

        assert plugin.config is None
        assert plugin.get_config("key") == "value"
        assert plugin.get_config("missing", "d") == "d"


class TestGetConfigCompat:
    """get_config 对 dict 与 model 两种存储的兼容。"""

    def test_get_config_reads_model_fields(self):
        """声明 config_cls 时 get_config 按字段访问，缺省回退 default。"""
        plugin = _make_plugin(_DemoPlugin)
        plugin.initialize(_Kernel({"demo": {"enabled": True}}))

        assert plugin.get_config("enabled") is True
        assert plugin.get_config("port") == 8080
        assert plugin.get_config("missing", "d") == "d"

    def test_config_property_model_vs_none(self):
        """config property：声明时返回 model，未声明时返回 None。"""
        demo = _make_plugin(_DemoPlugin)
        legacy = _make_plugin(_LegacyPlugin, name="legacy")

        demo.initialize(_Kernel({"demo": {}}))
        legacy.initialize(_Kernel({"legacy": {}}))

        assert isinstance(demo.config, _DemoConfig)
        assert legacy.config is None

    def test_config_default_instance_before_initialize(self):
        """声明 config_cls 时 __init__ 即持默认实例，initialize 前 config 可用。"""
        demo = _make_plugin(_DemoPlugin)

        assert isinstance(demo.config, _DemoConfig)
        assert demo.config.port == 8080
        assert demo.config.enabled is False
