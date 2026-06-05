"""
Skill 数据模型 —— 用户知识资产的定义。

Skill 不是 Plugin（研发侧代码扩展），
而是用户侧文档资产（SKILL.md）。
框架只读入并注入 System Prompt，不做业务理解。
"""

from __future__ import annotations

import logging
import shutil
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class SkillVisibility(str, Enum):
    """技能可见性"""
    PRIVATE = "private"
    SHARED = "shared"


class SkillMeta:
    """SKILL.md YAML frontmatter 元数据"""

    def __init__(self, **data: Any) -> None:
        self.name: str = str(data.get("name", ""))
        self.description: str = str(data.get("description", ""))
        self.author: str = str(data.get("author", ""))
        visibility_raw = data.get("visibility", "private")
        self.visibility: SkillVisibility = (
            SkillVisibility(visibility_raw)
            if isinstance(visibility_raw, str)
            else SkillVisibility.PRIVATE
        )
        self.version: str = str(data.get("version", "0.1.0"))
        self.based_on: str | None = data.get("based_on")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "visibility": self.visibility.value,
            "version": self.version,
        }
        if self.based_on:
            d["based_on"] = self.based_on
        return d


class Skill:
    """技能模型"""

    def __init__(self, meta: SkillMeta, body: str, file_path: Path) -> None:
        self.meta = meta
        self.body = body
        self.file_path = file_path


class SkillManager:
    """技能管理器

    钉在 skills/ 目录上，负责技能的 CRUD 和发现。
    不依赖 Plugin 生命周期，是纯文件管理。
    """

    def __init__(self, skills_base_dir: str | Path) -> None:
        self._skills_base_dir = Path(skills_base_dir).resolve()

    # ---- 内部路径 ----

    def _user_skills_dir(self, user_id: str) -> Path:
        return self._skills_base_dir / user_id

    def _skill_md_path(self, user_id: str, skill_name: str) -> Path:
        return self._user_skills_dir(user_id) / skill_name / "SKILL.md"

    # ---- CRUD ----

    def create(
        self,
        user_id: str,
        name: str,
        description: str,
        body: str,
        visibility: SkillVisibility = SkillVisibility.PRIVATE,
        based_on: str | None = None,
    ) -> Skill:
        """创建技能

        Args:
            user_id: 创建者用户 ID
            name: 技能名称（hyphen-case）
            description: 技能触发描述
            body: Markdown 指令正文
            visibility: 可见性，默认私有
            based_on: 派生来源（可选），格式 "author/skill_name"

        Returns:
            创建的 Skill 实例

        Raises:
            FileExistsError: 技能已存在
        """
        skill_dir = self._user_skills_dir(user_id) / name
        if skill_dir.exists():
            raise FileExistsError(f"技能 '{name}' 已存在")
        skill_dir.mkdir(parents=True)

        meta = SkillMeta(
            name=name, description=description,
            author=user_id, visibility=visibility,
            based_on=based_on,
        )
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(self._serialize(meta, body), encoding="utf-8")
        logger.info("用户 %s 创建技能 '%s' (type=%s)", user_id, name, visibility.value)
        return Skill(meta=meta, body=body, file_path=skill_md)

    def get(self, user_id: str, skill_name: str) -> Skill | None:
        """获取指定技能"""
        skill_md = self._skill_md_path(user_id, skill_name)
        if not skill_md.exists():
            return None
        return self._load_skill(skill_md)

    def list_skills(self, user_id: str) -> list[Skill]:
        """列出用户所有私有技能"""
        skills_dir = self._user_skills_dir(user_id)
        if not skills_dir.exists():
            return []
        result: list[Skill] = []
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir():
                skill = self._load_skill(child / "SKILL.md")
                if skill is not None:
                    result.append(skill)
        return result

    def delete(self, user_id: str, skill_name: str) -> bool:
        """删除指定技能

        Returns:
            是否删除成功
        """
        skill_dir = self._user_skills_dir(user_id) / skill_name
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        logger.info("用户 %s 删除技能 '%s'", user_id, skill_name)
        return True

    def update(
        self,
        user_id: str,
        skill_name: str,
        *,
        description: str | None = None,
        body: str | None = None,
        visibility: SkillVisibility | None = None,
    ) -> Skill | None:
        """更新技能（部分更新）

        Args:
            user_id: 用户 ID
            skill_name: 技能名称
            description: 新的描述（可选）
            body: 新的指令正文（可选）
            visibility: 新的可见性（可选）

        Returns:
            更新后的 Skill，未找到时返回 None
        """
        skill = self.get(user_id, skill_name)
        if skill is None:
            return None

        if description is not None:
            skill.meta.description = description
        if body is not None:
            skill.body = body
        if visibility is not None:
            skill.meta.visibility = visibility

        skill.file_path.write_text(
            self._serialize(skill.meta, skill.body),
            encoding="utf-8",
        )
        logger.info("用户 %s 更新技能 '%s'", user_id, skill_name)
        return skill

    # ---- 共享技能发现 ----

    def find_shared_skills(self) -> list[Skill]:
        """遍历所有用户的 skills/ 目录，收集 visibility=shared 的技能"""
        shared: list[Skill] = []
        if not self._skills_base_dir.exists():
            return shared
        for user_dir in self._skills_base_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for skill_dir in sorted(user_dir.iterdir()):
                skill = self._load_skill(skill_dir / "SKILL.md")
                if skill is not None and skill.meta.visibility == SkillVisibility.SHARED:
                    shared.append(skill)
        return shared

    # ---- 序列化 / 反序列化 ----

    def _load_skill(self, skill_md: Path) -> Skill | None:
        """从 SKILL.md 文件加载 Skill"""
        if not skill_md.exists():
            return None
        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = self._parse(content)
            return Skill(meta=meta, body=body, file_path=skill_md)
        except Exception:
            logger.exception("解析技能文件失败: %s", skill_md)
            return None

    @staticmethod
    def _serialize(meta: SkillMeta, body: str) -> str:
        """将 SkillMeta + body 序列化为 SKILL.md 格式"""
        front = yaml.dump(
            meta.to_dict(), allow_unicode=True,
            default_flow_style=False,
        ).strip()
        return f"---\n{front}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _parse(content: str) -> tuple[SkillMeta, str]:
        """解析 SKILL.md 中的 frontmatter + body"""
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("缺少 frontmatter 分隔符")
        end = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end == -1:
            raise ValueError("未找到 frontmatter 结束分隔符")
        front_text = "\n".join(lines[1:end])
        body = "\n".join(lines[end + 1:]).strip()
        front_data: dict[str, Any] = yaml.safe_load(front_text) or {}
        return SkillMeta(**front_data), body


__all__ = [
    "Skill",
    "SkillMeta",
    "SkillManager",
    "SkillVisibility",
]
