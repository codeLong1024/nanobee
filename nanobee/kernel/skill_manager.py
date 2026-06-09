"""技能加载器 —— 只负责扫描、加载、注入技能元数据。

Skill 是用户知识资产，不是代码插件。
框架只做两件事：
1. 发现技能（扫描 builtin + per-context 目录下的 SKILL.md）
2. 注入元数据（name + description → system prompt）

与旧版 SkillManager 的关键区别：
- 去除了 CRUD（用户通过 write_file 自主管理）
- 去除了全局用户技能扫描（~/.nanobee/skills/ 废弃）
- 改用 per-context 技能扫描（context/<user_id>/skills/）
- 新增内置技能扫描（nanobee/skills/）
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from nanobee.utils.logger import logger


class SkillMeta:
    """SKILL.md YAML frontmatter 元数据"""

    _DESC_MAX_LENGTH = 1024
    _FORBIDDEN_CHARS = "<>"

    def __init__(self, **data: Any) -> None:
        self.name: str = str(data.get("name", ""))
        self.description: str = str(data.get("description", ""))
        self.author: str = str(data.get("author", ""))
        self.compatibility: str | None = data.get("compatibility")
        self.full_inject: bool = bool(data.get("full_inject", False))
        self._validate_description()

    def _validate_description(self) -> None:
        if len(self.description) > self._DESC_MAX_LENGTH:
            raise ValueError(
                f"description 长度 {len(self.description)} 超过限制 {self._DESC_MAX_LENGTH}"
            )
        for ch in self._FORBIDDEN_CHARS:
            if ch in self.description:
                raise ValueError(f"description 包含禁止字符 '{ch}'")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "author": self.author,
        }
        if self.compatibility:
            d["compatibility"] = self.compatibility
        if self.full_inject:
            d["full_inject"] = True
        return d


class Skill:
    """技能模型"""

    def __init__(self, meta: SkillMeta, body: str, file_path: Path, source: str = "user") -> None:
        self.meta = meta
        self.body = body
        self.file_path = file_path
        self.source = source  # "builtin" | "user"


class SkillsLoader:
    """技能加载器

    扫描两个来源并合并呈现：
    1. 内置技能（nanobee/skills/）—— 框架打包，只读
    2. 用户技能（<context_root>/skills/）—— 通过 scan_context_skills() 按 context 加载

    注意：全局 `~/.nanobee/skills/` 路径已废弃，不再扫描。
    技能存放在每个用户的上下文目录下（`users/<user_id>/skills/`）。

    缓存策略：基于 mtime 的文件系统缓存，TTL 2 秒。
    """

    _CACHE_TTL = 2.0

    def __init__(
        self,
        user_skills_dir: str | Path | None = None,
        builtin_skills_dir: str | Path | None = None,
    ) -> None:
        self._user_dir = Path(user_skills_dir).resolve() if user_skills_dir else None
        self._builtin_dir = Path(builtin_skills_dir).resolve() if builtin_skills_dir else None

        # 缓存：key -> (skills 列表, mtime, 缓存时间)
        self._cache: dict[str, list[Skill]] = {}
        self._cache_time: dict[str, float] = {}
        self._dir_mtime: dict[str, float] = {}

    @property
    def builtin_dir(self) -> Path | None:
        """内置技能目录路径（只读，用于沙箱白名单）"""
        return self._builtin_dir

    # ---- 缓存管理 ----

    def _scan_dir_mtime(self, dir_path: Path) -> float:
        """获取目录的最新修改时间。"""
        if not dir_path.is_dir():
            return 0.0
        max_mtime = 0.0
        try:
            for root, _, files in os.walk(dir_path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        m = os.stat(fp).st_mtime
                        if m > max_mtime:
                            max_mtime = m
                    except OSError:
                        pass
        except OSError:
            pass
        return max_mtime

    def _is_cache_valid(self, key: str, dir_path: Path) -> bool:
        if key not in self._cache:
            return False
        if time.time() - self._cache_time.get(key, 0) > self._CACHE_TTL:
            return False
        cached_mtime = self._dir_mtime.get(key, 0)
        current_mtime = self._scan_dir_mtime(dir_path)
        if cached_mtime != current_mtime:
            self._cache.pop(key, None)
            self._cache_time.pop(key, None)
            self._dir_mtime.pop(key, None)
            return False
        return True

    # ---- 技能发现 ----

    def list_builtin_skills(self) -> list[Skill]:
        """列出所有内置技能。"""
        if not self._builtin_dir:
            return []
        key = "builtin"
        if self._is_cache_valid(key, self._builtin_dir):
            return self._cache[key]

        skills = self._scan_dir(self._builtin_dir, source="builtin")
        self._cache[key] = skills
        self._cache_time[key] = time.time()
        self._dir_mtime[key] = self._scan_dir_mtime(self._builtin_dir)
        return skills

    def list_user_skills(self) -> list[Skill]:
        """列出所有用户技能（废弃的全局路径）。

        .. deprecated::
            改用 scan_context_skills(context_root) 按 context 加载技能。
        """
        if not self._user_dir:
            return []
        key = "user"
        if self._is_cache_valid(key, self._user_dir):
            return self._cache[key]

        skills = self._scan_dir(self._user_dir, source="user")
        self._cache[key] = skills
        self._cache_time[key] = time.time()
        self._dir_mtime[key] = self._scan_dir_mtime(self._user_dir)
        return skills

    def scan_context_skills(self, context_root: Path) -> list[Skill]:
        """扫描指定 context 目录下的用户技能。

        Args:
            context_root: 用户上下文根目录（base_dir）

        Returns:
            技能列表，按技能名排序
        """
        skills_dir = context_root / "skills"
        if not skills_dir.is_dir():
            return []
        return self._scan_dir(skills_dir, source="user")

    def list_all_skills(self) -> list[Skill]:
        """列出所有技能（内置 + 用户），同名时双方都返回。"""
        builtin = self.list_builtin_skills()
        user = self.list_user_skills()
        # 同名时显示两个来源，LLM 自行判断
        return builtin + user

    def get_skill(self, name: str) -> Skill | None:
        """按名称查找技能（先查用户，再查内置）。"""
        for skill in self.list_user_skills():
            if skill.meta.name == name:
                return skill
        for skill in self.list_builtin_skills():
            if skill.meta.name == name:
                return skill
        return None

    def invalidate_cache(self) -> None:
        """清除缓存（用户通过 write_file 修改技能后调用）。"""
        self._cache.clear()
        self._cache_time.clear()
        self._dir_mtime.clear()

    # ---- 内部 ----

    def _scan_dir(self, base_dir: Path, *, source: str) -> list[Skill]:
        """扫描目录下的所有 SKILL.md。"""
        if not base_dir.is_dir():
            return []
        result: list[Skill] = []
        for child in sorted(base_dir.iterdir()):
            if child.is_dir():
                skill = self._load_skill(child / "SKILL.md", source=source)
                if skill is not None:
                    result.append(skill)
        return result

    def _load_skill(self, skill_md: Path, *, source: str) -> Skill | None:
        if not skill_md.exists():
            return None
        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = self._parse(content)
            return Skill(meta=meta, body=body, file_path=skill_md, source=source)
        except Exception:
            logger.exception("解析技能文件失败: {}", skill_md)
            return None

    @staticmethod
    def _serialize(meta: SkillMeta, body: str) -> str:
        front = yaml.dump(
            meta.to_dict(), allow_unicode=True, default_flow_style=False,
        ).strip()
        return f"---\n{front}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _parse(content: str) -> tuple[SkillMeta, str]:
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


# 向后兼容别名（已废弃，请使用 SkillsLoader）
SkillManager = SkillsLoader


__all__ = [
    "Skill",
    "SkillMeta",
    "SkillsLoader",
    "SkillManager",
]
