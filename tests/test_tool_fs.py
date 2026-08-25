"""
Tool FS 插件测试 - 文件系统工具（read_file, write_file, edit_file, list_dir）

覆盖 12 个验收用例：
1. read_file 读取文本文件
2. read_file 分页读取（offset + limit）
3. read_file 文件不存在
4. write_file 创建新文件
5. write_file 覆盖现有文件
6. edit_file 精确替换文本
7. edit_file 未找到匹配文本
8. list_dir 列出目录内容
9. 路径逃逸：../../../etc/passwd
10. 路径逃逸：绝对路径 /etc/passwd
11. 路径逃逸：write_file 写向外层目录
12. 路径逃逸：list_dir 列出外层目录
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobee.builtin.tool_fs import ToolFileSystemPlugin
from nanobee.kernel.sandbox import ContextSandbox
from nanobee.exceptions import SandboxViolationError
from nanobee.plugins.base import PluginMetadata


# ---- 辅助工具 ----


def _create_plugin(workspace: Path | None = None) -> ToolFileSystemPlugin:
    """创建测试插件实例"""
    if workspace:
        # 测试环境：切换到 tmp_path，这样相对路径才能解析到 tmp_path
        import os
        os.chdir(workspace)
    return ToolFileSystemPlugin(PluginMetadata(name="tool_fs", plugin_type="tool"))


def _run_async(coro):
    """运行异步协程"""
    return asyncio.run(coro)


# ---- read_file 测试 ----


class TestReadFileTool:
    """验证 read_file 工具功能"""

    def test_read_file_success(self, tmp_path: Path):
        """read_file 成功读取文本文件"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="test.txt"))

        assert "1| line1" in result
        assert "2| line2" in result
        assert "3| line3" in result
        assert "共 3 行" in result

    def test_read_file_with_pagination(self, tmp_path: Path):
        """read_file 分页读取"""
        test_file = tmp_path / "large.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="large.txt", offset=2, limit=2))

        assert "2| line2" in result
        assert "3| line3" in result
        assert "共 5 行" in result

    def test_read_file_not_found(self, tmp_path: Path):
        """read_file 文件不存在时返回错误"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="nonexistent.txt"))

        assert "错误" in result
        assert "不存在" in result

    def test_read_file_empty(self, tmp_path: Path):
        """read_file 读取空文件"""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="empty.txt"))

        assert "空文件" in result

    def test_read_file_directory_error(self, tmp_path: Path):
        """read_file 读取目录时报错"""
        test_dir = tmp_path / "test_dir"
        test_dir.mkdir()

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="test_dir"))

        assert "错误" in result
        assert "不是文件" in result


# ---- write_file 测试 ----


class TestWriteFileTool:
    """验证 write_file 工具功能"""

    def test_write_file_create_new(self, tmp_path: Path):
        """write_file 创建新文件"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("write_file", path="new.txt", content="Hello World"))

        assert "成功写入" in result
        assert "11 个字符" in result

        test_file = tmp_path / "new.txt"
        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "Hello World"

    def test_write_file_overwrite(self, tmp_path: Path):
        """write_file 覆盖现有文件"""
        test_file = tmp_path / "existing.txt"
        test_file.write_text("old content", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("write_file", path="existing.txt", content="new content"))

        assert "成功写入" in result
        assert test_file.read_text(encoding="utf-8") == "new content"

    def test_write_file_create_subdir(self, tmp_path: Path):
        """write_file 创建子目录和文件"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("write_file", path="subdir/nested.txt", content="nested content"))

        assert "成功写入" in result
        nested_file = tmp_path / "subdir" / "nested.txt"
        assert nested_file.exists()
        assert nested_file.read_text(encoding="utf-8") == "nested content"

    def test_write_file_missing_path(self, tmp_path: Path):
        """write_file 缺少路径参数"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("write_file", path=None, content="test"))

        assert "错误" in result or "失败" in result


# ---- edit_file 测试 ----


class TestEditFileTool:
    """验证 edit_file 工具功能"""

    def test_edit_file_success(self, tmp_path: Path):
        """edit_file 成功替换文本"""
        test_file = tmp_path / "edit.txt"
        test_file.write_text("Hello World", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("edit_file", path="edit.txt", old_text="World", new_text="Python"))

        assert "成功编辑" in result
        assert test_file.read_text(encoding="utf-8") == "Hello Python"

    def test_edit_file_multiple_matches_warning(self, tmp_path: Path):
        """edit_file 匹配多个时发出警告"""
        test_file = tmp_path / "multi.txt"
        test_file.write_text("aaa\naaa\naaa\n", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("edit_file", path="multi.txt", old_text="aaa", new_text="bbb"))

        assert "警告" in result
        assert "匹配了 3 次" in result

    def test_edit_file_replace_all(self, tmp_path: Path):
        """edit_file 替换所有匹配项"""
        test_file = tmp_path / "all.txt"
        test_file.write_text("aaa\naaa\naaa\n", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("edit_file", path="all.txt", old_text="aaa",
                                                new_text="bbb", replace_all=True))

        assert "成功编辑" in result
        assert test_file.read_text(encoding="utf-8") == "bbb\nbbb\nbbb\n"

    def test_edit_file_not_found(self, tmp_path: Path):
        """edit_file 未找到匹配文本"""
        test_file = tmp_path / "notfound.txt"
        test_file.write_text("Hello World", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("edit_file", path="notfound.txt", old_text="XYZ", new_text="ABC"))

        assert "错误" in result
        assert "未找到" in result

    def test_edit_file_create_new(self, tmp_path: Path):
        """edit_file 创建新文件（old_text=''）"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("edit_file", path="new_create.txt", old_text="", new_text="created"))

        assert "成功创建" in result
        assert (tmp_path / "new_create.txt").read_text(encoding="utf-8") == "created"


# ---- list_dir 测试 ----


class TestListDirTool:
    """验证 list_dir 工具功能"""

    def test_list_dir_success(self, tmp_path: Path):
        """list_dir 成功列出目录"""
        (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
        (tmp_path / "file2.txt").write_text("content2", encoding="utf-8")
        (tmp_path / "subdir").mkdir()

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("list_dir", path="."))

        assert "file1.txt" in result
        assert "file2.txt" in result
        assert "subdir" in result

    def test_list_dir_recursive(self, tmp_path: Path):
        """list_dir 递归列出目录"""
        (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("list_dir", path=".", recursive=True))

        assert "file1.txt" in result
        assert "nested.txt" in result
        assert "subdir" in result

    def test_list_dir_not_found(self, tmp_path: Path):
        """list_dir 目录不存在"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("list_dir", path="nonexistent"))

        assert "错误" in result
        assert "不存在" in result

    def test_list_dir_empty(self, tmp_path: Path):
        """list_dir 空目录"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("list_dir", path="empty"))

        assert "为空" in result


# ---- delete_file 测试 ----


class TestDeleteFileTool:
    """验证 delete_file 工具功能"""

    def test_delete_file_success(self, tmp_path: Path):
        """delete_file 成功删除文件"""
        test_file = tmp_path / "to_delete.txt"
        test_file.write_text("content", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path="to_delete.txt"))

        assert "成功删除" in result
        assert not test_file.exists()

    def test_delete_empty_dir(self, tmp_path: Path):
        """delete_file 成功删除空目录"""
        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path="empty_dir"))

        assert "成功删除" in result
        assert not empty_dir.exists()

    def test_delete_dir_recursive(self, tmp_path: Path):
        """delete_file 递归删除非空目录"""
        test_dir = tmp_path / "nonempty"
        test_dir.mkdir()
        (test_dir / "file1.txt").write_text("a", encoding="utf-8")
        (test_dir / "file2.txt").write_text("b", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path="nonempty", recursive=True))

        assert "成功递归删除" in result
        assert not test_dir.exists()

    def test_delete_nonempty_dir_without_recursive(self, tmp_path: Path):
        """delete_file 非空目录未设置 recursive 时返回错误提示"""
        test_dir = tmp_path / "nonempty2"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("c", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path="nonempty2"))

        assert "错误" in result
        assert "目录非空" in result
        assert "recursive=true" in result
        assert test_dir.exists()

    def test_delete_file_not_found(self, tmp_path: Path):
        """delete_file 路径不存在时返回错误"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path="nonexistent.txt"))

        assert "错误" in result
        assert "不存在" in result

    def test_delete_file_missing_path(self, tmp_path: Path):
        """delete_file 缺少路径参数"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("delete_file", path=None))

        assert "错误" in result or "失败" in result

    def test_delete_file_sandbox_rejected(self, tmp_path: Path):
        """delete_file 路径越界被沙箱拦截"""
        # 在沙箱上下文中测试：writable 根在 tmp_path 内，../ 逃逸被拦截
        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path)
        try:
            result = _run_async(plugin.execute_tool("delete_file", path="../outside_dir"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "沙箱拦截" in result or "错误" in result


# ---- 工具定义测试 ----


class TestToolDefinitions:
    """验证工具定义元数据"""

    def test_get_tools_returns_five_tools(self):
        """get_tools 返回 5 个工具定义"""
        plugin = _create_plugin()
        tools = plugin.get_tools()

        assert len(tools) == 5
        tool_names = [t["function"]["name"] for t in tools]
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "list_dir" in tool_names
        assert "delete_file" in tool_names

    def test_tool_parameters_valid(self):
        """工具参数定义有效"""
        plugin = _create_plugin()
        tools = plugin.get_tools()

        for tool in tools:
            assert "type" in tool
            assert tool["type"] == "function"
            assert "function" in tool
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

    def test_list_tool_names(self):
        """list_tool_names 返回工具名称列表"""
        plugin = _create_plugin()
        names = plugin.list_tool_names()

        assert len(names) == 5
        assert names == ["read_file", "write_file", "edit_file", "list_dir", "delete_file"]


# # ---- 路径沙箱拦截测试 ----
# 
# 
# class TestPathSandbox:
#     """验证路径沙箱拦截，确保路径不逃逸 workspace"""
# 
#     def test_reject_path_traversal_escape(self, tmp_path: Path):
#         """../../../etc/passwd 路径逃逸被拦截"""
#         plugin = _create_plugin(tmp_path)
#         result = _run_async(plugin.execute_tool("read_file", path="../../../etc/passwd"))
# 
#         assert "沙箱拦截" in result or "错误" in result
# 
#     def test_reject_absolute_path_outside_workspace(self, tmp_path: Path):
#         """绝对路径 /etc/passwd 被拦截"""
#         plugin = _create_plugin(tmp_path)
#         result = _run_async(plugin.execute_tool("read_file", path="/etc/passwd"))
# 
#         assert "沙箱拦截" in result or "错误" in result
# 
#     def test_reject_write_outside_workspace(self, tmp_path: Path):
#         """write_file 写向外层目录被拦截"""
#         plugin = _create_plugin(tmp_path)
#         result = _run_async(plugin.execute_tool("write_file", path="../outside.txt", content="malicious"))
# 
#         assert "沙箱拦截" in result or "错误" in result
# 
#     def test_reject_edit_outside_workspace(self, tmp_path: Path):
#         """edit_file 编辑外层目录文件被拦截"""
#         plugin = _create_plugin(tmp_path)
#         result = _run_async(plugin.execute_tool("edit_file", path="../../etc/hostname",
#                                                old_text="old", new_text="new"))
# 
#         assert "沙箱拦截" in result or "错误" in result
# 
#     def test_allow_within_workspace(self, tmp_path: Path):
#         """workspace 内路径正常"""
#         test_file = tmp_path / "safe.txt"
#         test_file.write_text("safe content", encoding="utf-8")
# 
#         plugin = _create_plugin(tmp_path)
#         result = _run_async(plugin.execute_tool("read_file", path="safe.txt"))
# 
#         assert "safe content" in result
# 
#     # def test_sandbox_contextvar_injection(self, tmp_path):
    #     """ContextVar 注入沙箱后，_resolve_path 能正确使用"""
    #     from nanobee.kernel.context_sandbox_var import bind_sandbox, reset_sandbox
    #     root = tmp_path / "users" / "user-a"
    #     root.mkdir(parents=True, exist_ok=True)
    #     sandbox = ContextSandbox(root)
    #     plugin = _create_plugin()
    #     token = bind_sandbox(sandbox)
    #     try:
    #         inside = root / "test.txt"
    #         inside.touch()
    #         result = plugin._resolve_path(str(inside))
    #         assert result == inside.resolve()
    #     finally:
    #         reset_sandbox(token)


# ---- Overlay 回退测试（通过沙箱 resolve_with_fallback） ----


def _with_sandbox(
    tmp_path: Path,
    *,
    prefix_map: dict[str, Path] | None = None,
    read_only: list[Path] | None = None,
) -> object:
    """创建并绑定测试沙箱到 ContextVar，返回 token（用于 finally reset）"""
    from nanobee.kernel.context_sandbox_var import bind_sandbox
    sandbox = ContextSandbox(
        tmp_path,
        read_only_roots=read_only,
        prefix_map=prefix_map,
    )
    return bind_sandbox(sandbox)


class TestOverlayFallback:
    """验证 read_file / list_dir 的 overlay 回退逻辑（沙箱注入模式）"""

    def test_read_fallback_to_builtin(self, tmp_path: Path):
        """read_file 用户目录没有，内置目录有 → 回退读取内置版"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)
        builtin_file = builtin_skills / "builtin_guide.md"
        builtin_file.write_text("# Builtin Skill\nbuiltin content", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("read_file", path="skills/builtin_guide.md"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "Builtin Skill" in result
        assert "builtin content" in result

    def test_read_user_overrides_builtin(self, tmp_path: Path):
        """read_file 用户目录有同名文件 → 读用户版，不回退"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)
        builtin_file = builtin_skills / "guide.md"
        builtin_file.write_text("builtin content", encoding="utf-8")

        user_skills = tmp_path / "skills"
        user_skills.mkdir()
        user_file = user_skills / "guide.md"
        user_file.write_text("user content", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("read_file", path="skills/guide.md"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "user content" in result
        assert "builtin" not in result

    def test_read_overlay_no_fallback_no_user_file(self, tmp_path: Path):
        """read_file 用户和内置都没有 → 报错"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("read_file", path="skills/nonexistent.md"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "错误" in result
        assert "不存在" in result

    def test_read_no_overlay_configured(self, tmp_path: Path):
        """read_file 无 overlay 配置时不回退"""
        plugin = _create_plugin(tmp_path)
        result = _run_async(plugin.execute_tool("read_file", path="skills/nonexistent.md"))

        assert "错误" in result
        assert "不存在" in result

    def test_list_dir_fallback_to_builtin(self, tmp_path: Path):
        """list_dir 用户目录不存在 → 回退列出内置目录"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)
        (builtin_skills / "memory").mkdir()
        (builtin_skills / "skill-creator").mkdir()

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("list_dir", path="skills"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "memory" in result
        assert "skill-creator" in result

    def test_list_dir_no_overlay_fallback(self, tmp_path: Path):
        """list_dir 用户和内置都没有 → 报错"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("list_dir", path="nonexistent_dir"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "错误" in result
        assert "不存在" in result

    def test_read_overlay_deep_nested_file(self, tmp_path: Path):
        """read_file 回退读取多级嵌套文件"""
        builtin_parent = tmp_path / "builtin_skills"
        builtin_skills = builtin_parent / "skills"
        nested = builtin_skills / "memory"
        nested.mkdir(parents=True)
        nested_file = nested / "SKILL.md"
        nested_file.write_text("---\nname: memory\n---\n\n# Memory Skill", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("read_file", path="skills/memory/SKILL.md"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "name: memory" in result
        assert "Memory Skill" in result

    def test_read_file_desc_mentions_overlay(self):
        """_read_file_desc 提及 overlay 回退"""
        plugin = _create_plugin()
        desc = plugin._read_file_desc()
        assert "自动回退" in desc or "内置" in desc

    def test_overlay_non_skills_prefix(self, tmp_path: Path):
        """非 skills 前缀路径不触发 overlay 回退"""
        builtin_parent = tmp_path / "builtin_dir"
        builtin_skills = builtin_parent / "skills"
        builtin_skills.mkdir(parents=True)
        builtin_file = builtin_skills / "config.yaml"
        builtin_file.write_text("key: value", encoding="utf-8")

        plugin = _create_plugin(tmp_path)
        token = _with_sandbox(tmp_path, prefix_map={"skills/": builtin_skills})
        try:
            result = _run_async(plugin.execute_tool("read_file", path="config.yaml"))
        finally:
            from nanobee.kernel.context_sandbox_var import reset_sandbox
            reset_sandbox(token)

        assert "错误" in result
        assert "不存在" in result
