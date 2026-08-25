"""技能校验器测试"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobee.kernel.skill_validator import (
    ALLOWED_PROPERTIES,
    check_allowed_properties,
    validate_skill_meta,
    validate_skill_name,
)


class TestValidateSkillName:
    """validate_skill_name 测试"""

    def test_valid_snake_case(self):
        validate_skill_name("my_skill")
        validate_skill_name("git_log_analyzer")
        validate_skill_name("a")
        validate_skill_name("a1b2c3")
        validate_skill_name("node_18_upgrade")
        validate_skill_name("skill_creator")

    def test_invalid_uppercase(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("My_Skill")

    def test_invalid_trailing_underscore(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("my_skill_")

    def test_invalid_leading_underscore(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("_memory")

    def test_invalid_consecutive_underscores(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("my__skill")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("")

    def test_invalid_hyphen(self):
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_name("my-skill")


class TestValidateSkillMeta:
    """validate_skill_meta 测试"""

    def test_valid_meta(self):
        meta = SimpleNamespace(name="my_skill", description="A useful skill")
        validate_skill_meta(meta)  # should not raise

    def test_missing_name(self):
        meta = SimpleNamespace(name="", description="desc")
        with pytest.raises(ValueError, match="缺少名称"):
            validate_skill_meta(meta)

    def test_missing_description(self):
        meta = SimpleNamespace(name="my_skill", description="")
        with pytest.raises(ValueError, match="缺少描述"):
            validate_skill_meta(meta)

    def test_invalid_name_format(self):
        meta = SimpleNamespace(name="Bad_Name", description="desc")
        with pytest.raises(ValueError, match="snake_case"):
            validate_skill_meta(meta)


class TestCheckAllowedProperties:
    """check_allowed_properties 测试"""

    def test_all_allowed(self):
        props = {"name": "test", "description": "desc", "license": "MIT", "compatibility": "anthropic"}
        extra = check_allowed_properties(props)
        assert extra == []

    def test_extra_property(self):
        props = {"name": "test", "unknown-field": "value"}
        extra = check_allowed_properties(props)
        assert extra == ["unknown-field"]

    def test_nanobee_prefix_allowed(self):
        props = {"name": "test", "nanobee/custom": "yes"}
        extra = check_allowed_properties(props)
        assert extra == []

    def test_allowed_properties_set(self):
        """确保 ALLOWED_PROPERTIES 包含兼容 Anthropic 所需的字段。"""
        assert "compatibility" in ALLOWED_PROPERTIES
        assert "license" in ALLOWED_PROPERTIES
        assert "allowed-tools" in ALLOWED_PROPERTIES
        assert "metadata" in ALLOWED_PROPERTIES
