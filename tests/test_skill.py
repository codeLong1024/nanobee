"""Skill 数据模型与管理器测试"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.kernel.skill_manager import SkillManager, SkillMeta, SkillVisibility


class TestSkillMeta:
    """SkillMeta 数据模型测试"""

    def test_default_values(self):
        meta = SkillMeta(name="test", description="测试技能")
        assert meta.name == "test"
        assert meta.description == "测试技能"
        assert meta.author == ""
        assert meta.visibility == SkillVisibility.PRIVATE
        assert meta.version == "0.1.0"
        assert meta.based_on is None

    def test_shared_visibility(self):
        meta = SkillMeta(name="test", description="", visibility="shared")
        assert meta.visibility == SkillVisibility.SHARED

    def test_based_on(self):
        meta = SkillMeta(name="test", description="", based_on="alice/original")
        assert meta.based_on == "alice/original"

    def test_to_dict(self):
        meta = SkillMeta(name="my-skill", description="A skill",
                         author="bob", visibility="shared",
                         version="1.0.0", based_on="alice/original")
        d = meta.to_dict()
        assert d["name"] == "my-skill"
        assert d["visibility"] == "shared"
        assert d["based_on"] == "alice/original"


class TestSkillManagerCRUD:
    """SkillManager CRUD 基础操作测试"""

    def test_create_skill(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill = skill_mgr.create("alice", "my-skill", "测试技能", "这是技能内容")

        assert skill.meta.name == "my-skill"
        assert skill.meta.description == "测试技能"
        assert skill.meta.author == "alice"
        assert skill.meta.visibility == SkillVisibility.PRIVATE

        skill_md = tmp_path / "skills" / "alice" / "my-skill" / "SKILL.md"
        assert skill_md.exists()
        content = skill_md.read_text(encoding="utf-8")
        assert "name: my-skill" in content
        assert "这是技能内容" in content

    def test_create_shared_skill(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill = skill_mgr.create("alice", "shared-skill", "共享技能",
                                 "内容", visibility=SkillVisibility.SHARED)
        assert skill.meta.visibility == SkillVisibility.SHARED

        content = skill.file_path.read_text(encoding="utf-8")
        assert "visibility: shared" in content

    def test_create_duplicate_raises(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "测试", "内容")
        with pytest.raises(FileExistsError):
            skill_mgr.create("alice", "my-skill", "重复", "内容")

    def test_get_skill(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "测试", "内容")
        retrieved = skill_mgr.get("alice", "my-skill")
        assert retrieved is not None
        assert retrieved.meta.name == "my-skill"
        assert retrieved.body == "内容"

    def test_get_nonexistent(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        result = skill_mgr.get("alice", "nonexistent")
        assert result is None

    def test_list_no_skills(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skills = skill_mgr.list_skills("alice")
        assert skills == []

    def test_create_and_list(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "skill-1", "技能1", "内容1")
        skill_mgr.create("alice", "skill-2", "技能2", "内容2")

        skills = skill_mgr.list_skills("alice")
        assert len(skills) == 2
        names = [s.meta.name for s in skills]
        assert "skill-1" in names
        assert "skill-2" in names

    def test_delete_skill(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "测试", "内容")
        assert skill_mgr.delete("alice", "my-skill") is True
        assert skill_mgr.get("alice", "my-skill") is None

    def test_delete_nonexistent(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        assert skill_mgr.delete("alice", "nonexistent") is False

    def test_list_other_user_not_affected(self, tmp_path: Path):
        """A 创建的技能不影响 B 的技能列表。"""
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "alice-skill", "A 的技能", "内容")
        skill_mgr.create("bob", "bob-skill", "B 的技能", "内容")

        assert len(skill_mgr.list_skills("alice")) == 1
        assert len(skill_mgr.list_skills("bob")) == 1
        assert len(skill_mgr.list_skills("charlie")) == 0


class TestSkillManagerUpdate:
    """SkillManager 更新操作测试"""

    def test_update_description(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "旧描述", "内容")
        updated = skill_mgr.update("alice", "my-skill", description="新描述")
        assert updated is not None
        assert updated.meta.description == "新描述"

        # 验证已持久化
        reloaded = skill_mgr.get("alice", "my-skill")
        assert reloaded is not None
        assert reloaded.meta.description == "新描述"

    def test_update_body(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "测试", "旧内容")
        updated = skill_mgr.update("alice", "my-skill", body="新内容")
        assert updated is not None
        assert updated.body == "新内容"

    def test_update_visibility(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "my-skill", "测试", "内容")
        updated = skill_mgr.update("alice", "my-skill",
                                   visibility=SkillVisibility.SHARED)
        assert updated is not None
        assert updated.meta.visibility == SkillVisibility.SHARED

    def test_update_nonexistent(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        result = skill_mgr.update("alice", "nonexistent", description="新描述")
        assert result is None


class TestSkillManagerShared:
    """共享技能发现测试"""

    def test_find_shared_skills(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "private-skill", "私有", "内容",
                         visibility=SkillVisibility.PRIVATE)
        skill_mgr.create("alice", "shared-skill", "共享", "内容",
                         visibility=SkillVisibility.SHARED)

        shared = skill_mgr.find_shared_skills()
        assert len(shared) == 1
        assert shared[0].meta.name == "shared-skill"

    def test_private_skill_not_shared(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "private-skill", "私有", "内容")
        assert skill_mgr.find_shared_skills() == []

    def test_multiple_users_shared(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "alice-shared", "A 共享", "内容",
                         visibility=SkillVisibility.SHARED)
        skill_mgr.create("bob", "bob-shared", "B 共享", "内容",
                         visibility=SkillVisibility.SHARED)

        shared = skill_mgr.find_shared_skills()
        assert len(shared) == 2

    def test_author_field_set_correctly(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "alice-shared", "A 共享", "内容",
                         visibility=SkillVisibility.SHARED)
        skill_mgr.create("bob", "bob-shared", "B 共享", "内容",
                         visibility=SkillVisibility.SHARED)

        shared = skill_mgr.find_shared_skills()
        authors = {s.meta.author for s in shared}
        assert authors == {"alice", "bob"}


class TestSkillManagerFork:
    """Fork 机制测试"""

    def test_fork_with_based_on(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        original = skill_mgr.create(
            "alice", "original-skill", "原始技能", "原始内容",
            visibility=SkillVisibility.SHARED,
        )
        fork = skill_mgr.create(
            "bob", "forked-skill", "Fork 版本", "修改后的内容",
            based_on=f"{original.meta.author}/{original.meta.name}",
        )
        assert fork.meta.based_on == "alice/original-skill"
        assert fork.meta.author == "bob"

    def test_fork_different_name(self, tmp_path: Path):
        skill_mgr = SkillManager(tmp_path / "skills")
        skill_mgr.create("alice", "src", "源", "内容",
                         visibility=SkillVisibility.SHARED)
        fork = skill_mgr.create(
            "bob", "my-version", "我的版本", "修改",  # different name
            based_on="alice/src",
        )
        assert fork.meta.name == "my-version"
        assert fork.meta.based_on == "alice/src"


class TestSkillSerialization:
    """SKILL.md 序列化/反序列化测试"""

    def test_serialize_deserialize_roundtrip(self, tmp_path: Path):
        """创建技能后读取，验证 roundtrip"""
        skill_mgr = SkillManager(tmp_path / "skills")

        expected_body = (
            "分析 git 日志的步骤：\n\n"
            "1. 获取最近 30 天的提交\n"
            "2. 按作者分组统计\n"
            "3. 生成变更摘要"
        )
        skill_mgr.create(
            "alice", "git-analyzer",
            "分析 git 提交历史",
            expected_body,
            visibility=SkillVisibility.SHARED,
        )

        reloaded = skill_mgr.get("alice", "git-analyzer")
        assert reloaded is not None
        assert reloaded.meta.name == "git-analyzer"
        assert reloaded.meta.description == "分析 git 提交历史"
        assert reloaded.meta.visibility == SkillVisibility.SHARED
        assert reloaded.meta.author == "alice"
        assert reloaded.body == expected_body

    def test_parse_invalid_frontmatter(self):
        """无效的 frontmatter 返回 None"""
        skill_mgr = SkillManager("/tmp/nonexistent")
        # 手动加载不存在的文件
        fake_path = Path("/tmp/nonexistent/SKILL.md")
        result = skill_mgr._load_skill(fake_path)
        assert result is None

    def test_empty_contexts_dir_shared(self, tmp_path: Path):
        """contexts 目录不存在时 find_shared_skills 返回空列表。"""
        skill_mgr = SkillManager(tmp_path / "nonexistent")
        assert skill_mgr.find_shared_skills() == []

    def test_missing_user_dir_list(self, tmp_path: Path):
        """用户目录不存在时 list_skills 返回空列表。"""
        skill_mgr = SkillManager(tmp_path / "skills")
        assert skill_mgr.list_skills("nonexistent") == []
