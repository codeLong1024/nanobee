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

    def test_valid_kebab_case(self):
        validate_skill_name("my-skill")
        validate_skill_name("git-log-analyzer")
        validate_skill_name("a")
        validate_skill_name("a1b2c3")
        validate_skill_name("node-18-upgrade")
        validate_skill_name("skill-creator")

    def test_invalid_uppercase(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("My-Skill")

    def test_invalid_trailing_hyphen(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("my-skill-")

    def test_invalid_leading_hyphen(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("-memory")

    def test_invalid_consecutive_hyphens(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("my--skill")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("")

    def test_invalid_underscore(self):
        with pytest.raises(ValueError, match="kebab-case"):
            validate_skill_name("my_skill")


class TestValidateSkillMeta:
    """validate_skill_meta 测试"""

    def test_valid_meta(self):
        meta = SimpleNamespace(name="my-skill", description="A useful skill")
        validate_skill_meta(meta)  # should not raise

    def test_missing_name(self):
        meta = SimpleNamespace(name="", description="desc")
        with pytest.raises(ValueError, match="缺少名称"):
            validate_skill_meta(meta)

    def test_missing_description(self):
        meta = SimpleNamespace(name="my-skill", description="")
        with pytest.raises(ValueError, match="缺少描述"):
            validate_skill_meta(meta)

    def test_invalid_name_format(self):
        meta = SimpleNamespace(name="Bad_Name", description="desc")
        with pytest.raises(ValueError, match="kebab-case"):
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
