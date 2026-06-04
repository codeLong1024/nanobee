"""技能管理工具 —— 用户通过对话创建/编辑/删除技能

这些工具不依赖插件系统，直接操作 SkillManager 管理 SKILL.md 文件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobee.agent.tools.base import Tool
from nanobee.plugins.skill import SkillManager, SkillVisibility

if TYPE_CHECKING:
    pass


class CreateSkillTool(Tool):
    """创建个性化技能"""

    name = "create_skill"
    description = (
        "创建个性化技能。技能是用户自己的知识资产，"
        "由用户通过对话自由创建，可设为私有或共享给其他用户。"
    )

    def __init__(self, skill_manager: SkillManager) -> None:
        self._skill_mgr = skill_manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "创建者用户 ID",
                },
                "name": {
                    "type": "string",
                    "description": "技能名称（hyphen-case，如 git-log-analyzer）",
                },
                "description": {
                    "type": "string",
                    "description": "技能触发描述，简短说明技能用途",
                },
                "body": {
                    "type": "string",
                    "description": "Markdown 格式的指令正文，详细描述技能行为和步骤",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["private", "shared"],
                    "description": "可见性，private 仅自己可用，shared 对所有用户可见",
                },
                "based_on": {
                    "type": "string",
                    "description": "派生来源（可选），格式 author/skill_name，表示 Fork 自某个共享技能",
                },
            },
            "required": ["user_id", "name", "description", "body", "visibility"],
        }

    async def execute(self, **kwargs: Any) -> str:
        visibility = SkillVisibility(kwargs.get("visibility", "private"))
        skill = self._skill_mgr.create(
            user_id=kwargs["user_id"],
            name=kwargs["name"],
            description=kwargs["description"],
            body=kwargs["body"],
            visibility=visibility,
            based_on=kwargs.get("based_on"),
        )
        return (
            f"技能 '{skill.meta.name}' 创建成功（{skill.meta.visibility.value}）\n"
            f"描述：{skill.meta.description}"
        )


class ListSkillsTool(Tool):
    """列出当前用户的技能和可用的共享技能"""

    name = "list_skills"
    description = "列出当前用户的所有技能（含其他用户共享的技能）。"

    def __init__(self, skill_manager: SkillManager) -> None:
        self._skill_mgr = skill_manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID",
                },
            },
            "required": ["user_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        user_id = kwargs["user_id"]

        private = self._skill_mgr.list_skills(user_id)
        shared = self._skill_mgr.find_shared_skills()

        lines: list[str] = []

        if private:
            lines.append("## 我的技能")
            for s in private:
                tag = "🔓" if s.meta.visibility == SkillVisibility.SHARED else "🔒"
                lines.append(f"- {tag} **{s.meta.name}**: {s.meta.description}")

        others_shared = [s for s in shared if s.meta.author != user_id]
        if others_shared:
            lines.append("\n## 共享技能（来自其他用户）")
            for s in others_shared:
                based = f" (Fork 自 {s.meta.based_on})" if s.meta.based_on else ""
                lines.append(f"- 🌍 **{s.meta.name}** (@{s.meta.author}): {s.meta.description}{based}")

        if not private and not others_shared:
            return "暂无可用技能。使用 create_skill 创建你的第一个技能。"

        return "\n".join(lines)


class UpdateSkillTool(Tool):
    """更新技能（描述、正文、可见性）"""

    name = "update_skill"
    description = "更新已有技能的描述、指令正文或可见性设置。"

    def __init__(self, skill_manager: SkillManager) -> None:
        self._skill_mgr = skill_manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID",
                },
                "name": {
                    "type": "string",
                    "description": "技能名称",
                },
                "description": {
                    "type": "string",
                    "description": "新的技能描述（可选）",
                },
                "body": {
                    "type": "string",
                    "description": "新的指令正文（可选）",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["private", "shared"],
                    "description": "新的可见性（必填，必须显式指定）",
                },
            },
            "required": ["user_id", "name", "visibility"],
        }

    async def execute(self, **kwargs: Any) -> str:
        # visibility 是必填参数，语义核心，不允许静默跳过
        visibility = SkillVisibility(kwargs["visibility"])

        skill = self._skill_mgr.update(
            user_id=kwargs["user_id"],
            skill_name=kwargs["name"],
            description=kwargs.get("description"),
            body=kwargs.get("body"),
            visibility=visibility,
        )
        if skill is None:
            return f"错误：技能 '{kwargs['name']}' 未找到"
        return f"技能 '{skill.meta.name}' 已更新（可见性: {skill.meta.visibility.value}）"


class DeleteSkillTool(Tool):
    """删除用户技能"""

    name = "delete_skill"
    description = "删除用户的某个技能。此操作不可恢复。"

    def __init__(self, skill_manager: SkillManager) -> None:
        self._skill_mgr = skill_manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID",
                },
                "name": {
                    "type": "string",
                    "description": "要删除的技能名称",
                },
            },
            "required": ["user_id", "name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        success = self._skill_mgr.delete(
            user_id=kwargs["user_id"],
            skill_name=kwargs["name"],
        )
        if not success:
            return f"错误：技能 '{kwargs['name']}' 未找到"
        return f"技能 '{kwargs['name']}' 已删除"


class ForkSkillTool(Tool):
    """Fork 共享技能到自己的技能列表中"""

    name = "fork_skill"
    description = (
        "基于其他用户的共享技能创建自己的副本，"
        "Fork 后可以自由修改。记录 based_on 来源。"
    )

    def __init__(self, skill_manager: SkillManager) -> None:
        self._skill_mgr = skill_manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "当前用户 ID",
                },
                "source_author": {
                    "type": "string",
                    "description": "技能原作者 user_id",
                },
                "source_name": {
                    "type": "string",
                    "description": "要 Fork 的技能名称",
                },
                "new_name": {
                    "type": "string",
                    "description": "新技能名称（可选，默认同源技能名）",
                },
            },
            "required": ["user_id", "source_author", "source_name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        source_author = kwargs["source_author"]
        source_name = kwargs["source_name"]
        new_name = kwargs.get("new_name", source_name)

        # 从共享技能中找到源技能
        shared = self._skill_mgr.find_shared_skills()
        source: Any = None
        for s in shared:
            if s.meta.author == source_author and s.meta.name == source_name:
                source = s
                break

        if source is None:
            return (
                f"错误：共享技能 '{source_name}'（作者: {source_author}）未找到"
            )

        new_skill = self._skill_mgr.create(
            user_id=kwargs["user_id"],
            name=new_name,
            description=source.meta.description,
            body=source.body,
            visibility=SkillVisibility.PRIVATE,
            based_on=f"{source_author}/{source_name}",
        )
        return (
            f"技能 '{new_name}' 已从 {source_author}/{source_name} Fork 创建成功\n"
            f"描述：{new_skill.meta.description}"
        )


__all__ = [
    "CreateSkillTool",
    "ListSkillsTool",
    "UpdateSkillTool",
    "DeleteSkillTool",
    "ForkSkillTool",
]
