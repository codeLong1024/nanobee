"""Schema.additionalProperties 校验单元测试。

覆盖 validate_json_schema_value、_cast_object、validate_params 三个层面的
additionalProperties 约束执行。
"""

from __future__ import annotations

import pytest

from nanobee.agent.tools.base import Schema, Tool


# -- validate_json_schema_value 测试 ------------------------------------------------

class TestValidateJsonSchemaValue:

    def test_additional_properties_false_allows_known_keys(self):
        """additionalProperties: false 时，已知属性应通过校验。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test"}, schema, "params"
        )
        assert errors == []

    def test_additional_properties_false_rejects_unknown_key(self):
        """additionalProperties: false 时，未知属性应报错。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "typo_name": "oops"}, schema, "params"
        )
        assert len(errors) == 1
        assert "unknown property params.typo_name" in errors[0]
        assert "additionalProperties not allowed" in errors[0]

    def test_additional_properties_true_allows_unknown_key(self):
        """additionalProperties: true（默认）时，未知属性不报错。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": True,
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "extra": 123}, schema, "params"
        )
        assert errors == []

    def test_additional_properties_unspecified_allows_unknown_key(self):
        """未指定 additionalProperties（默认 true）时，未知属性不报错。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "extra": 123}, schema, "params"
        )
        assert errors == []

    def test_additional_properties_dict_validates_unknown_keys(self):
        """additionalProperties 为 dict 时，对未知属性执行子 schema 校验。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": {"type": "integer"},
        }
        # 合法未知属性
        errors = Schema.validate_json_schema_value(
            {"name": "test", "score": 42}, schema, "params"
        )
        assert errors == []

    def test_additional_properties_dict_rejects_invalid_unknown_key_type(self):
        """additionalProperties 为 dict 时，类型不匹配的未知属性应报错。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": {"type": "integer"},
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "score": "not-a-number"}, schema, "params"
        )
        assert len(errors) >= 1
        assert any("should be integer" in e for e in errors)

    def test_additional_properties_false_multiple_unknown_keys(self):
        """additionalProperties: false 时，多个未知属性各报一个错误。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "a": 1, "b": 2}, schema, "params"
        )
        assert len(errors) == 2
        assert "params.a" in errors[0]
        assert "params.b" in errors[1]

    def test_required_checked_even_with_additional_properties_false(self):
        """required 检查与 additionalProperties 检查可同时触发。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["age"],
            "additionalProperties": False,
        }
        errors = Schema.validate_json_schema_value(
            {"name": "test", "extra": "bad"}, schema, "params"
        )
        assert len(errors) == 2
        assert any("missing required" in e for e in errors)
        assert any("unknown property" in e for e in errors)

    def test_nested_object_additional_properties_false(self):
        """嵌套 object 中 additionalProperties: false 被递归执行。"""
        schema = {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "additionalProperties": False,
                }
            },
        }
        errors = Schema.validate_json_schema_value(
            {"config": {"key": "val", "bad": 1}}, schema, "params"
        )
        assert len(errors) == 1
        assert "unknown property params.config.bad" in errors[0]


# -- _cast_object 测试 ------------------------------------------------------------

class DummyCastTool(Tool):
    """用于测试 _cast_object 的最小 Tool 子类。"""

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "用于测试 _cast_object"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return None


class TestCastObject:

    def setup_method(self):
        self.tool = DummyCastTool()

    def test_additional_properties_false_strips_unknown_keys(self):
        """additionalProperties: false 时，未知键应被剔除。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        result = self.tool._cast_object(
            {"name": "test", "typo": "bad"}, schema
        )
        assert result == {"name": "test"}

    def test_additional_properties_true_keeps_unknown_keys(self):
        """additionalProperties: true 时，未知键原样保留。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": True,
        }
        result = self.tool._cast_object(
            {"name": "test", "extra": "keep"}, schema
        )
        assert result == {"name": "test", "extra": "keep"}

    def test_additional_properties_unspecified_keeps_unknown_keys(self):
        """未指定 additionalProperties 时，未知键原样保留。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }
        result = self.tool._cast_object(
            {"name": "test", "extra": "keep"}, schema
        )
        assert result == {"name": "test", "extra": "keep"}

    def test_additional_properties_dict_casts_unknown_values(self):
        """additionalProperties 为 dict 时，对未知值执行类型转换。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": {"type": "integer"},
        }
        result = self.tool._cast_object(
            {"name": "test", "count": "42"}, schema
        )
        assert result == {"name": "test", "count": 42}

    def test_additional_properties_false_all_unknown_stripped(self):
        """additionalProperties: false + 全部是未知键 → 返回空 dict。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        result = self.tool._cast_object(
            {"bad_a": 1, "bad_b": 2}, schema
        )
        assert result == {}

    def test_non_dict_input_passthrough(self):
        """非 dict 输入原样返回（防御性）。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
        result = self.tool._cast_object("not-a-dict", schema)
        assert result == "not-a-dict"


# -- validate_params 集成测试 -----------------------------------------------------

class DummyParamTool(Tool):
    """带 parameters schema 的 Tool 子类，用于集成测试 validate_params。"""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema

    @property
    def name(self) -> str:
        return "dummy_param"

    @property
    def description(self) -> str:
        return "用于集成测试 validate_params"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> Any:
        return None


class TestValidateParamsIntegration:

    def test_additional_properties_false_extra_param_errors(self):
        """validate_params 对 additionalProperties: false 报出未知属性错误。"""
        tool = DummyParamTool({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        })
        errors = tool.validate_params({"path": "/tmp", "typo_path": "/bad"})
        assert len(errors) == 1
        assert "unknown property typo_path" in errors[0]

    def test_additional_properties_true_extra_param_ok(self):
        """validate_params 对 additionalProperties: true 不报错。"""
        tool = DummyParamTool({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": True,
        })
        errors = tool.validate_params({"path": "/tmp", "extra": 123})
        assert errors == []

    def test_cast_params_then_validate_extra_params_stripped(self):
        """cast_params 先剔除未知参数，validate_params 再校验应通过。"""
        tool = DummyParamTool({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        })
        casted = tool.cast_params({"path": "/tmp", "typo": "bad"})
        # cast_params 已剔除 typo
        assert "typo" not in casted
        assert casted == {"path": "/tmp"}
        # validate_params 不应报错
        errors = tool.validate_params(casted)
        assert errors == []
