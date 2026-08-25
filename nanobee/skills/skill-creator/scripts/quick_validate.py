#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能校验器 —— 校验 nanobee 技能文件夹结构和必需的 frontmatter。

用法:
    python quick_validate.py <skill_directory>

注意: nanobee 技能使用 kebab-case 命名（如 skill-creator、git-log-analyzer）。
"""

import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

MAX_SKILL_NAME_LENGTH = 64
# nanobee 支持的 frontmatter 字段
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "author",
    "compatibility",
    "full_inject",
}
ALLOWED_RESOURCE_DIRS = {"scripts", "references", "assets"}
PLACEHOLDER_MARKERS = ("[todo", "todo:")
_DESC_MAX_LENGTH = 1024
_FORBIDDEN_CHARS = "<>"

# nanobee 的 kebab-case 正则：以小写字母开头，只含小写字母、数字、连字符
KEBAB_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _extract_frontmatter(content: str) -> Optional[str]:
    """提取 YAML frontmatter。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parse_simple_frontmatter(frontmatter_text: str) -> Optional[dict[str, str]]:
    """当 PyYAML 不可用时的回退解析器。"""
    parsed: dict[str, str] = {}
    current_key: Optional[str] = None

    for raw_line in frontmatter_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        is_indented = raw_line[:1].isspace()
        if is_indented:
            if current_key is None:
                return None
            current_value = parsed[current_key]
            parsed[current_key] = f"{current_value}\n{stripped}" if current_value else stripped
            continue

        if ":" not in stripped:
            return None

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            return None

        if value in {"|", ">"}:
            parsed[key] = ""
            current_key = key
            continue

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        parsed[key] = value
        current_key = key

    return parsed


def _load_frontmatter(frontmatter_text: str) -> tuple[Optional[dict], Optional[str]]:
    """加载 YAML frontmatter。"""
    if yaml is not None:
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            return None, f"frontmatter YAML 格式无效: {exc}"
        if not isinstance(frontmatter, dict):
            return None, "frontmatter 必须是一个 YAML 字典"
        return frontmatter, None

    frontmatter = _parse_simple_frontmatter(frontmatter_text)
    if frontmatter is None:
        return None, "frontmatter YAML 格式无效（未安装 PyYAML）"
    return frontmatter, None


def _validate_skill_name(name: str, folder_name: str) -> Optional[str]:
    """校验技能名称是否为合法的 kebab-case。"""
    if not KEBAB_CASE_RE.match(name):
        return (
            f"名称 '{name}' 应为 kebab-case "
            "（小写字母、数字、连字符，以小写字母开头，不能有连续连字符或末尾连字符）"
        )
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return (
            f"名称过长 ({len(name)} 字符)。"
            f" 最大允许 {MAX_SKILL_NAME_LENGTH} 字符。"
        )
    if name != folder_name:
        return f"技能名称 '{name}' 必须与目录名称 '{folder_name}' 一致"
    return None


def _validate_description(description: str) -> Optional[str]:
    """校验描述内容。"""
    trimmed = description.strip()
    if not trimmed:
        return "description 不能为空"
    lowered = trimmed.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return "description 仍包含 TODO 占位文本"
    for ch in _FORBIDDEN_CHARS:
        if ch in trimmed:
            return "description 不能包含尖括号（< 或 >）"
    if len(trimmed) > _DESC_MAX_LENGTH:
        return f"description 过长 ({len(trimmed)} 字符)。最大允许 {_DESC_MAX_LENGTH} 字符。"
    return None


def validate_skill(skill_path: str) -> tuple[bool, str]:
    """校验技能文件夹结构和必需的 frontmatter。

    Args:
        skill_path: 技能文件夹路径

    Returns:
        (是否通过, 消息)
    """
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists():
        return False, f"技能文件夹不存在: {skill_path}"
    if not skill_path.is_dir():
        return False, f"路径不是目录: {skill_path}"

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md 不存在"

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"无法读取 SKILL.md: {exc}"

    frontmatter_text = _extract_frontmatter(content)
    if frontmatter_text is None:
        return False, "frontmatter 格式无效（缺少 --- 分隔符）"

    frontmatter, error = _load_frontmatter(frontmatter_text)
    if error:
        return False, error

    # 检查未预期的字段
    unexpected_keys = sorted(set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected_keys:
        allowed = ", ".join(sorted(ALLOWED_FRONTMATTER_KEYS))
        unexpected = ", ".join(unexpected_keys)
        return (
            False,
            f"SKILL.md frontmatter 中存在未预期的字段: {unexpected}。"
            f" 允许的字段: {allowed}",
        )

    if "name" not in frontmatter:
        return False, "frontmatter 缺少 'name'"
    if "description" not in frontmatter:
        return False, "frontmatter 缺少 'description'"

    name = frontmatter["name"]
    if not isinstance(name, str):
        return False, f"name 必须是字符串，实际类型为 {type(name).__name__}"
    name_error = _validate_skill_name(name.strip(), skill_path.name)
    if name_error:
        return False, name_error

    description = frontmatter["description"]
    if not isinstance(description, str):
        return False, f"description 必须是字符串，实际类型为 {type(description).__name__}"
    description_error = _validate_description(description)
    if description_error:
        return False, description_error

    # 校验 full_inject（如果存在）
    full_inject = frontmatter.get("full_inject")
    if full_inject is not None and not isinstance(full_inject, bool):
        return False, f"'full_inject' 必须是布尔值，实际类型为 {type(full_inject).__name__}"

    # 校验资源目录
    for child in skill_path.iterdir():
        if child.name == "SKILL.md":
            continue
        if child.is_dir() and child.name in ALLOWED_RESOURCE_DIRS:
            continue
        if child.is_symlink():
            continue
        return (
            False,
            f"技能根目录中存在未预期的文件或目录: {child.name}。"
            " 只允许 SKILL.md、scripts/、references/ 和 assets/。",
        )

    return True, "技能校验通过！"


def main() -> None:
    if len(sys.argv) != 2:
        print("用法: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()
