"""SkillManager 缓存性能测试

验证基于 mtime 的文件系统缓存是否正确工作，以及性能提升效果。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nanobee.kernel.skill_manager import SkillManager, SkillVisibility


class TestSkillManagerCache:
    """验证 SkillManager 缓存功能。"""

    def test_cache_hits_after_first_read(self, tmp_path: Path) -> None:
        """第一次读取后，第二次读取应命中缓存。"""
        user_id = "alice"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建技能
        skill_mgr.create(user_id, "test-skill", "Test", "Body", SkillVisibility.PRIVATE)

        # 第一次读取（未缓存）
        start = time.time()
        result1 = skill_mgr.list_skills(user_id)
        time1 = time.time() - start

        # 第二次读取（应命中缓存）
        start = time.time()
        result2 = skill_mgr.list_skills(user_id)
        time2 = time.time() - start

        assert len(result1) == 1
        assert len(result2) == 1
        # 缓存命中应更快（理论上）
        assert time2 <= time1

    def test_cache_invalidates_on_create(self, tmp_path: Path) -> None:
        """创建技能后，缓存应被清除。"""
        user_id = "alice"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建第一个技能
        skill_mgr.create(user_id, "skill-1", "S1", "Body1", SkillVisibility.PRIVATE)

        # 读取（缓存）
        skills1 = skill_mgr.list_skills(user_id)
        assert len(skills1) == 1

        # 创建第二个技能
        skill_mgr.create(user_id, "skill-2", "S2", "Body2", SkillVisibility.PRIVATE)

        # 读取应包含两个技能（缓存已清除）
        skills2 = skill_mgr.list_skills(user_id)
        assert len(skills2) == 2

    def test_cache_invalidates_on_delete(self, tmp_path: Path) -> None:
        """删除技能后，缓存应被清除。"""
        user_id = "alice"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建两个技能
        skill_mgr.create(user_id, "skill-1", "S1", "Body1", SkillVisibility.PRIVATE)
        skill_mgr.create(user_id, "skill-2", "S2", "Body2", SkillVisibility.PRIVATE)

        # 读取（缓存）
        skills1 = skill_mgr.list_skills(user_id)
        assert len(skills1) == 2

        # 删除一个技能
        skill_mgr.delete(user_id, "skill-1")

        # 读取应只包含一个技能
        skills2 = skill_mgr.list_skills(user_id)
        assert len(skills2) == 1

    def test_cache_invalidates_on_update(self, tmp_path: Path) -> None:
        """更新技能后，缓存应被清除。"""
        user_id = "alice"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建技能
        skill_mgr.create(user_id, "test-skill", "Original", "Body", SkillVisibility.PRIVATE)

        # 读取（缓存）
        skills1 = skill_mgr.list_skills(user_id)
        assert skills1[0].meta.description == "Original"

        # 更新技能
        skill_mgr.update(user_id, "test-skill", description="Updated")

        # 读取应包含更新后的描述
        skills2 = skill_mgr.list_skills(user_id)
        assert skills2[0].meta.description == "Updated"

    def test_shared_skills_cache(self, tmp_path: Path) -> None:
        """共享技能缓存应正常工作。"""
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建共享技能
        skill_mgr.create("alice", "shared-skill", "Shared", "Body", SkillVisibility.SHARED)

        # 第一次读取
        shared1 = skill_mgr.find_shared_skills()
        assert len(shared1) == 1

        # 第二次读取（应命中缓存）
        shared2 = skill_mgr.find_shared_skills()
        assert len(shared2) == 1
        assert shared1[0].meta.name == shared2[0].meta.name

    def test_shared_skills_cache_invalidates_on_visibility_change(self, tmp_path: Path) -> None:
        """共享技能可见性变更后，缓存应被清除。"""
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建私有技能
        skill_mgr.create("alice", "private-skill", "Private", "Body", SkillVisibility.PRIVATE)

        # 读取共享技能（应为空）
        shared1 = skill_mgr.find_shared_skills()
        assert len(shared1) == 0

        # 更新为共享
        skill_mgr.update("alice", "private-skill", visibility=SkillVisibility.SHARED)

        # 读取应包含共享技能
        shared2 = skill_mgr.find_shared_skills()
        assert len(shared2) == 1

    def test_cache_ttl_expiration(self, tmp_path: Path) -> None:
        """缓存 TTL 过期后应重新读取。"""
        from nanobee.kernel.skill_manager import SkillManager as SM

        user_id = "alice"
        # 使用极短的 TTL 用于测试
        SM._CACHE_TTL = 0.1

        try:
            skill_mgr = SkillManager(tmp_path / "skills")
            skill_mgr.create(user_id, "test-skill", "Test", "Body", SkillVisibility.PRIVATE)

            # 第一次读取
            skills1 = skill_mgr.list_skills(user_id)
            assert len(skills1) == 1

            # 等待 TTL 过期
            time.sleep(0.15)

            # 修改文件
            skill_md = tmp_path / "skills" / user_id / "test-skill" / "SKILL.md"
            skill_md.write_text(
                "---\nname: test-skill\ndescription: Updated\nauthor: alice\nvisibility: private\n---\n\nNew Body\n",
                encoding="utf-8",
            )

            # 读取应重新加载
            skills2 = skill_mgr.list_skills(user_id)
            assert skills2[0].meta.description == "Updated"
        finally:
            # 恢复原始 TTL
            SM._CACHE_TTL = 2.0

    def test_cache_empty_list(self, tmp_path: Path) -> None:
        """空列表也应被缓存。"""
        user_id = "nonexistent"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 读取不存在的用户
        skills1 = skill_mgr.list_skills(user_id)
        assert skills1 == []

        # 再次读取应命中缓存
        skills2 = skill_mgr.list_skills(user_id)
        assert skills2 == []

    def test_multi_user_isolation(self, tmp_path: Path) -> None:
        """多用户技能应隔离缓存。"""
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建用户 A 的技能
        skill_mgr.create("alice", "alice-skill", "Alice", "Body", SkillVisibility.PRIVATE)

        # 创建用户 B 的技能
        skill_mgr.create("bob", "bob-skill", "Bob", "Body", SkillVisibility.PRIVATE)

        # 读取用户 A 的技能
        alice_skills = skill_mgr.list_skills("alice")
        assert len(alice_skills) == 1
        assert alice_skills[0].meta.name == "alice-skill"

        # 读取用户 B 的技能
        bob_skills = skill_mgr.list_skills("bob")
        assert len(bob_skills) == 1
        assert bob_skills[0].meta.name == "bob-skill"

    def test_cache_performance_improvement(self, tmp_path: Path) -> None:
        """缓存应显著提升读取性能。"""
        user_id = "user"
        skill_mgr = SkillManager(tmp_path / "skills")

        # 创建 10 个技能
        for i in range(10):
            skill_mgr.create(
                user_id,
                f"skill-{i}",
                f"Skill {i}",
                f"Body {i} " * 100,  # 增加文件大小
                SkillVisibility.PRIVATE,
            )

        # 预热缓存
        skill_mgr.list_skills(user_id)

        # 测量缓存命中时间
        iterations = 100
        start = time.time()
        for _ in range(iterations):
            skills = skill_mgr.list_skills(user_id)
        cached_time = time.time() - start

        # 清除缓存
        skill_mgr._invalidate_cache()

        # 测量缓存未命中时间
        start = time.time()
        for _ in range(iterations):
            skills = skill_mgr.list_skills(user_id)
        uncached_time = time.time() - start

        # 缓存命中应更快
        assert cached_time < uncached_time
        # 理论上至少快 1.05 倍（CI 环境下更合理）
        speedup = uncached_time / cached_time if cached_time > 0 else float('inf')
        assert speedup >= 1.05, f"缓存加速比不足: {speedup:.2f}x"
