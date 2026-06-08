"""SkillsLoader 数据模型与双源发现测试"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.kernel.skill_manager import SkillMeta, SkillsLoader, Skill


def _make_skill_md(base_dir: Path, name: str, description: str, body: str,
                   author: str = "") -> Path:
    """在 base_dir 下创建测试 SKILL.md"""
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta_lines = ["---"]
    meta_lines.append(f"name: {name}")
    meta_lines.append(f"description: {description}")
    if author:
        meta_lines.append(f"author: {author}")
    meta_lines.append("---")
    content = "\n".join(meta_lines) + f"\n\n{body}\n"
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


class TestSkillMeta:
    """SkillMeta 数据模型测试"""

    def test_default_values(self):
        meta = SkillMeta(name="test", description="测试技能")
        assert meta.name == "test"
        assert meta.description == "测试技能"
        assert meta.author == ""

    def test_to_dict(self):
        meta = SkillMeta(name="my-skill", description="A skill", author="bob")
        d = meta.to_dict()
        assert d["name"] == "my-skill"
        assert d["description"] == "A skill"
        assert d["author"] == "bob"

    def test_compatibility_in_dict(self):
        meta = SkillMeta(name="test", description="test", compatibility="anthropic")
        d = meta.to_dict()
        assert d["compatibility"] == "anthropic"

    def test_description_exceeds_max_length(self):
        with pytest.raises(ValueError, match="超过限制"):
            SkillMeta(name="test", description="x" * 1025)

    def test_description_contains_angle_bracket(self):
        with pytest.raises(ValueError, match="禁止字符 '<'"):
            SkillMeta(name="test", description="含有 < 的描述")

    def test_description_boundary_ok(self):
        meta = SkillMeta(name="test", description="x" * 1024)
        assert len(meta.description) == 1024


class TestSkillsLoaderScan:
    """SkillsLoader 扫描功能测试"""

    def test_empty_user_skills(self, tmp_path: Path):
        loader = SkillsLoader(tmp_path / "skills")
        assert loader.list_user_skills() == []
        assert loader.list_all_skills() == []

    def test_scan_user_skills(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "skill-a", "描述 A", "内容 A")
        _make_skill_md(tmp_path / "skills", "skill-b", "描述 B", "内容 B")

        loader = SkillsLoader(tmp_path / "skills")
        skills = loader.list_user_skills()
        assert len(skills) == 2
        names = {s.meta.name for s in skills}
        assert names == {"skill-a", "skill-b"}

    def test_get_skill_by_name(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "my-skill", "My Skill", "content")
        loader = SkillsLoader(tmp_path / "skills")
        skill = loader.get_skill("my-skill")
        assert skill is not None
        assert skill.meta.description == "My Skill"

    def test_get_skill_nonexistent(self, tmp_path: Path):
        loader = SkillsLoader(tmp_path / "skills")
        assert loader.get_skill("nonexistent") is None

    def test_flat_structure(self, tmp_path: Path):
        """技能目录扁平化：skills/<name>/SKILL.md"""
        _make_skill_md(tmp_path / "skills", "alice-skill", "Alice 技能", "内容")
        _make_skill_md(tmp_path / "skills", "bob-skill", "Bob 技能", "内容")

        loader = SkillsLoader(tmp_path / "skills")
        skills = loader.list_user_skills()
        assert len(skills) == 2
        assert {s.meta.name for s in skills} == {"alice-skill", "bob-skill"}


class TestSkillsLoaderDualSource:
    """双源发现测试（builtin + user）"""

    def test_builtin_skills_loaded(self, tmp_path: Path):
        _make_skill_md(tmp_path / "builtin", "_memory", "内置记忆", "记忆策略")
        _make_skill_md(tmp_path / "builtin", "skill_creator", "技能创建", "创建指南")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )
        builtin = loader.list_builtin_skills()
        assert len(builtin) == 2
        assert {s.meta.name for s in builtin} == {"_memory", "skill_creator"}

    def test_list_all_merges_both_sources(self, tmp_path: Path):
        _make_skill_md(tmp_path / "builtin", "builtin-1", "内置", "内置内容")
        _make_skill_md(tmp_path / "skills", "user-1", "用户", "用户内容")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )
        all_skills = loader.list_all_skills()
        assert len(all_skills) == 2
        sources = {s.source for s in all_skills}
        assert sources == {"builtin", "user"}

    def test_skill_source_tag(self, tmp_path: Path):
        _make_skill_md(tmp_path / "builtin", "builtin-1", "内置", "内容")
        _make_skill_md(tmp_path / "skills", "user-1", "用户", "内容")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )
        for s in loader.list_all_skills():
            assert s.source in ("builtin", "user")

    def test_get_prefers_user_over_builtin(self, tmp_path: Path):
        _make_skill_md(tmp_path / "builtin", "my-skill", "内置版描述", "内置版")
        _make_skill_md(tmp_path / "skills", "my-skill", "用户版描述", "用户版")

        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )
        skill = loader.get_skill("my-skill")
        assert skill is not None
        # get_skill 优先返回用户版
        assert skill.source == "user"
        assert skill.meta.description == "用户版描述"


class TestSkillsLoaderCache:
    """SkillsLoader 缓存功能测试"""

    def test_cache_hits_on_second_read(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "test-skill", "Test", "Body")
        loader = SkillsLoader(tmp_path / "skills")

        skills1 = loader.list_user_skills()
        assert len(skills1) == 1

        skills2 = loader.list_user_skills()
        assert len(skills2) == 1

    def test_cache_invalidates(self, tmp_path: Path):
        loader = SkillsLoader(tmp_path / "skills")

        assert loader.list_user_skills() == []

        # 写入新技能
        _make_skill_md(tmp_path / "skills", "new-skill", "New", "Body")

        skills = loader.list_user_skills()
        assert len(skills) == 1

    def test_invalidate_cache_force(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "skill-1", "S1", "Body")
        loader = SkillsLoader(tmp_path / "skills")
        assert len(loader.list_user_skills()) == 1

        # invalidation 后重新扫描
        loader.invalidate_cache()
        assert len(loader.list_user_skills()) == 1

    def test_cache_empty_list(self, tmp_path: Path):
        loader = SkillsLoader(tmp_path / "skills")
        assert loader.list_user_skills() == []
        assert loader.list_user_skills() == []  # hit cache

    def test_builtin_cache_separate(self, tmp_path: Path):
        _make_skill_md(tmp_path / "builtin", "builtin-1", "B", "Body")
        loader = SkillsLoader(
            user_skills_dir=tmp_path / "skills",
            builtin_skills_dir=tmp_path / "builtin",
        )

        assert len(loader.list_builtin_skills()) == 1
        assert len(loader.list_user_skills()) == 0


class TestSkillSerialization:
    """SKILL.md 序列化/反序列化测试"""

    def test_parse_content(self):
        content = "---\nname: test\ndescription: 测试\n---\n\n正文内容\n"
        meta, body = SkillsLoader._parse(content)
        assert meta.name == "test"
        assert meta.description == "测试"
        assert body == "正文内容"

    def test_serialize_roundtrip(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "my-skill", "描述", "body content")
        loader = SkillsLoader(tmp_path / "skills")
        skill = loader.get_skill("my-skill")
        assert skill is not None
        assert skill.body == "body content"
        assert skill.meta.description == "描述"

    def test_invalid_frontmatter_skipped(self, tmp_path: Path):
        bad_dir = tmp_path / "skills" / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")
        _make_skill_md(tmp_path / "skills", "good", "好的", "内容")

        loader = SkillsLoader(tmp_path / "skills")
        skills = loader.list_user_skills()
        # 坏的 frontmatter 被静默跳过
        assert len(skills) == 1
        assert skills[0].meta.name == "good"


class TestSkillBackwardCompat:
    """向后兼容测试：SkillManager 别名与 Skill 模型"""

    def test_skill_manager_alias(self):
        from nanobee.kernel.skill_manager import SkillManager
        assert SkillManager is SkillsLoader

    def test_skill_model_has_source(self, tmp_path: Path):
        _make_skill_md(tmp_path / "skills", "test", "测试", "body")
        loader = SkillsLoader(tmp_path / "skills")
        skill = loader.get_skill("test")
        assert skill is not None
        assert hasattr(skill, "source")
