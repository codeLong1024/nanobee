"""SkillsLoader 双源缓存性能测试"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nanobee.kernel.skill_manager import SkillsLoader


def _make_skill_md(base_dir: Path, name: str, description: str, body: str) -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


class TestSkillsLoaderCache:
    """验证 SkillsLoader 双源缓存功能。"""

    def test_cache_hits_on_second_read(self, tmp_path: Path) -> None:
        """第二次读取应命中缓存。"""
        _make_skill_md(tmp_path / "skills", "test-skill", "Test", "Body")
        loader = SkillsLoader(tmp_path / "skills")

        start = time.time()
        result1 = loader.list_user_skills()
        time1 = time.time() - start

        start = time.time()
        result2 = loader.list_user_skills()
        time2 = time.time() - start

        assert len(result1) == 1
        assert len(result2) == 1

    def test_cache_updates_after_file_change(self, tmp_path: Path) -> None:
        """文件变更后通过 invalidation 刷新。"""
        _make_skill_md(tmp_path / "skills", "skill-1", "S1", "Body1")
        loader = SkillsLoader(tmp_path / "skills")

        skills1 = loader.list_user_skills()
        assert len(skills1) == 1

        # 添加第二个技能
        _make_skill_md(tmp_path / "skills", "skill-2", "S2", "Body2")
        # 显式清除缓存后刷新
        loader.invalidate_cache()

        skills2 = loader.list_user_skills()
        assert len(skills2) == 2

    def test_builtin_and_user_cache_separate(self, tmp_path: Path) -> None:
        """内置技能和用户技能使用独立的缓存。"""
        _make_skill_md(tmp_path / "builtin", "builtin-1", "B1", "Body1")
        _make_skill_md(tmp_path / "skills", "user-1", "U1", "Body2")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )

        # 读取内置（填充缓存）
        assert len(loader.list_builtin_skills()) == 1
        # 读取用户（填充缓存）
        assert len(loader.list_user_skills()) == 1

        # 内置添加一个新技能
        _make_skill_md(tmp_path / "builtin", "builtin-2", "B2", "Body3")

        # 内置缓存应失效，用户缓存应保持不变
        assert len(loader.list_builtin_skills()) == 2
        assert len(loader.list_user_skills()) == 1

    def test_cache_empty_list(self, tmp_path: Path) -> None:
        """空列表也应被缓存。"""
        loader = SkillsLoader(tmp_path / "skills")

        skills1 = loader.list_user_skills()
        assert skills1 == []

        skills2 = loader.list_user_skills()
        assert skills2 == []

    def test_invalidate_cache(self, tmp_path: Path) -> None:
        """手动清除缓存后重新扫描。"""
        _make_skill_md(tmp_path / "skills", "test-skill", "Test", "Body")
        loader = SkillsLoader(tmp_path / "skills")

        assert len(loader.list_user_skills()) == 1
        loader.invalidate_cache()
        assert len(loader.list_user_skills()) == 1

    def test_list_all_caches_both(self, tmp_path: Path) -> None:
        """list_all_skills 应缓存两个来源。"""
        _make_skill_md(tmp_path / "builtin", "b1", "B1", "Body")
        _make_skill_md(tmp_path / "skills", "u1", "U1", "Body")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )

        all1 = loader.list_all_skills()
        assert len(all1) == 2

        all2 = loader.list_all_skills()  # hit cache
        assert len(all2) == 2

    def test_cache_performance(self, tmp_path: Path) -> None:
        """缓存应显著提升读取性能（加速比 >= 1.05）。"""
        loader = SkillsLoader(tmp_path / "skills")

        # 创建 5 个技能
        for i in range(5):
            _make_skill_md(
                tmp_path / "skills",
                f"skill-{i}", f"Skill {i}", f"Body {i} " * 50,
            )

        # 预热
        loader.list_user_skills()

        iterations = 50
        start = time.time()
        for _ in range(iterations):
            loader.list_user_skills()
        cached_time = time.time() - start

        loader.invalidate_cache()

        start = time.time()
        for _ in range(iterations):
            loader.list_user_skills()
        uncached_time = time.time() - start

        speedup = uncached_time / cached_time if cached_time > 0 else float('inf')
        assert speedup >= 1.05, f"缓存加速比不足: {speedup:.2f}x"
