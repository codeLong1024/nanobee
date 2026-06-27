"""技能加载器 —— 只负责扫描、加载、注入技能元数据。

Skill 是用户知识资产，不是代码插件。
框架只做两件事：
1. 发现技能（扫描 builtin + instance + per-context 目录下的 SKILL.md）
2. 注入元数据（name + description → system prompt）

三层技能架构：
- L1 内置技能（nanobee/skills/）：框架打包，所有实例共享
- L2 实例技能（<data_dir>/skills/）：管理员配属，实例内所有用户共享
- L3 用户技能（<context_root>/skills/）：用户个人技能

同名优先级：L3 > L2 > L1

与旧版 SkillManager 的关键区别：
- 去除了 CRUD（用户通过 write_file 自主管理）
- 去除了全局用户技能扫描（~/.nanobee/skills/ 废弃）
- 改用 per-context 技能扫描（context/<user_id>/skills/）
- 新增内置技能扫描（nanobee/skills/）
- 新增实例级技能扫描（<data_dir>/skills/）
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

    扫描三个来源并合并呈现：
    1. 内置技能（nanobee/skills/）—— 框架打包，只读
    2. 实例技能（<data_dir>/skills/）—— 管理员配属，实例内所有用户共享，只读
    3. 用户技能（<context_root>/skills/）—— 通过 scan_context_skills() 按 context 加载

    注意：全局 `~/.nanobee/skills/` 路径已废弃，不再扫描。
    技能存放在每个用户的上下文目录下（`users/<user_id>/skills/`）。

    同名优先级：用户 > 实例 > 内置

    缓存策略：基于 mtime 的文件系统缓存，TTL 2 秒。
    """

    _CACHE_TTL = 2.0

    def __init__(
        self,
        user_skills_dir: str | Path | None = None,
        builtin_skills_dir: str | Path | None = None,
        instance_skills_dir: str | Path | None = None,
        enabled_instance_skills: list[str] | None = None,
    ) -> None:
        self._user_dir = Path(user_skills_dir).resolve() if user_skills_dir else None
        self._builtin_dir = Path(builtin_skills_dir).resolve() if builtin_skills_dir else None
        self._instance_dir = Path(instance_skills_dir).resolve() if instance_skills_dir else None
        # 部署方声明的实例技能白名单：None 或空列表=全部注入，非空=仅注入列表中的
        self._enabled_instance = enabled_instance_skills or []

        # 缓存：key -> (skills 列表, mtime, 缓存时间)
        self._cache: dict[str, list[Skill]] = {}
        self._cache_time: dict[str, float] = {}
        self._dir_mtime: dict[str, float] = {}

    @property
    def builtin_dir(self) -> Path | None:
        """内置技能目录路径（只读，用于沙箱白名单）"""
        return self._builtin_dir

    @property
    def instance_dir(self) -> Path | None:
        """实例级技能目录路径（只读，管理员配属，用于沙箱白名单）"""
        return self._instance_dir

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
        """列出所有用户技能（skills 目录下的技能）。

        与 scan_context_skills 的区别：此方法列出用户全局 skills 目录下的技能，
        用于技能管理（如 /list_skills 工具），而 scan_context_skills 按用户上下文
        目录加载，用于 Agent 运行时注入。
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

    def scan_instance_skills(self) -> list[Skill]:
        """扫描实例级技能目录下的所有技能。

        实例技能由管理员配属，对实例内所有用户共享（只读）。
        通过 ``instance_skills_dir`` 构造参数指定路径。

        Returns:
            技能列表，按技能名排序
        """
        if not self._instance_dir:
            return []
        key = "instance"
        if self._is_cache_valid(key, self._instance_dir):
            return self._cache[key]

        skills = self._scan_dir(self._instance_dir, source="instance")
        self._cache[key] = skills
        self._cache_time[key] = time.time()
        self._dir_mtime[key] = self._scan_dir_mtime(self._instance_dir)
        return skills

    def list_filtered_instance_skills(self) -> list[Skill]:
        """列出实例技能，按 enabled_instance_skills 白名单过滤。

        当 enabled_instance_skills 为空列表时，返回全部实例技能（向后兼容）。
        当 enabled_instance_skills 非空时，仅返回列表中指定的技能。

        Returns:
            过滤后的实例技能列表
        """
        all_instance = self.scan_instance_skills()
        if not self._enabled_instance:
            return all_instance
        enabled_set = set(self._enabled_instance)
        return [s for s in all_instance if s.meta.name in enabled_set]

    def get_enabled_instance_dirs(self) -> list[Path]:
        """获取已启用的实例技能目录路径列表。

        部署方声明了哪些技能，返回对应的目录绝对路径。
        调用方（如进程沙箱）可将这些路径用于只读挂载等用途。

        当 enabled_instance_skills 为空时，返回所有实例技能目录。
        当 enabled_instance_skills 非空时，仅返回列表中指定的目录。

        Returns:
            已启用实例技能的目录绝对路径列表
        """
        if not self._instance_dir or not self._instance_dir.is_dir():
            return []
        skills = self.list_filtered_instance_skills()
        return [s.file_path.parent.resolve() for s in skills]

    def list_all_skills(self) -> list[Skill]:
        """列出所有技能（内置 + 实例 + 用户），同名时双方都返回。

        优先级由调用方处理：用户 > 实例 > 内置
        """
        builtin = self.list_builtin_skills()
        instance = self.scan_instance_skills()
        user = self.list_user_skills()
        return builtin + instance + user

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
            if not child.is_symlink() and child.is_dir():
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
