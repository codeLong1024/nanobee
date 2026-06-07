"""技能管理工具 —— 用户通过文件操作自己管理技能

框架只提供 list_skills 一个辅助工具（发现可用技能）。
创建/编辑/删除由用户通过 write_file / delete_file 自主完成。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobee.agent.tools.base import Tool
from nanobee.kernel.skill_manager import SkillsLoader

if TYPE_CHECKING:
    pass


class ListSkillsTool(Tool):
    """列出所有可用技能（含内置和用户技能）"""

    name = "list_skills"
    description = "列出所有可用的技能（含内置和用户创建的技能）。"

    def __init__(self, loader: SkillsLoader) -> None:
        self._loader = loader

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        all_skills = self._loader.list_all_skills()
        if not all_skills:
            return "暂无可用技能。使用 write_file 在 `skills/<名称>/SKILL.md` 创建你的第一个技能。"

        lines: list[str] = ["## 可用技能\n"]
        for s in all_skills:
            tag = "[builtin]" if s.source == "builtin" else "[user]"
            base = f"- {tag} **{s.meta.name}**: {s.meta.description}"
            if s.meta.author:
                base += f" (@{s.meta.author})"
            lines.append(base)

        return "\n".join(lines)


__all__ = [
    "ListSkillsTool",
]
