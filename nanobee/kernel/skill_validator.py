"""技能校验器

提供 SKILL.md frontmatter 完整性检查和名称/描述合规校验。

与 SkillMeta.__init__ 中的 description 约束形成两层防线：
- SkillMeta 层：基础字段合法性校验（长度、字符）
- Validator 层：业务语义校验（frontmatter 完整性、name kebab-case）
"""

from __future__ import annotations

import re
from typing import Any

from nanobee.utils.logger import logger


# 允许的 frontmatter 顶级字段（白名单）
ALLOWED_PROPERTIES: frozenset[str] = frozenset({
    "name",
    "description",
    "author",
    "version",
    "compatibility",
    "license",
    "allowed-tools",
    "metadata",
})

# kebab-case 正则：仅小写字母、数字、连字符，小写字母开头，无连续连字符
# 与 skill-creator 工具链（init_skill.py / quick_validate.py）保持一致
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def validate_skill_name(name: str) -> None:
    """校验技能名称必须为 kebab-case。

    Args:
        name: 技能名称

    Raises:
        ValueError: 名称不符合 kebab-case 规范。
    """
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"技能名称 '{name}' 必须为 kebab-case（仅小写字母、数字、连字符，"
            f"小写字母开头，无连续连字符）"
        )


def validate_skill_meta(meta: Any) -> None:
    """校验 SkillMeta 的业务完整性。

    检查项：
    1. name 必须为非空 kebab-case 字符串
    2. description 必须非空
    3. frontmatter 不应包含白名单外的字段

    Args:
        meta: SkillMeta 实例（或类 dict 对象）

    Raises:
        ValueError: 校验失败。
    """
    # --- name ---
    name: str = getattr(meta, "name", "") or ""
    if not name:
        raise ValueError("技能缺少名称（name）")
    validate_skill_name(name)

    # --- description ---
    desc: str = getattr(meta, "description", "") or ""
    if not desc:
        raise ValueError("技能缺少描述（description）")


def check_allowed_properties(properties: dict[str, Any]) -> list[str]:
    """检查 frontmatter 中是否有白名单外的字段。

    Args:
        properties: frontmatter 键值对

    Returns:
        不允许的属性名称列表（空列表表示全部合规）
    """
    extra: list[str] = []
    for key in properties:
        # 允许 nanobot/nanobee 开头的扩展字段
        if key not in ALLOWED_PROPERTIES and not key.startswith(("nanobot/", "nanobee/")):
            extra.append(key)
    if extra:
        logger.warning("frontmatter 包含非标准字段: {}", extra)
    return extra


__all__ = [
    "ALLOWED_PROPERTIES",
    "check_allowed_properties",
    "validate_skill_meta",
    "validate_skill_name",
]
