"""
Tool Shell 沙箱修复测试

验证 tool_shell 的 L2 沙箱注入和 working_dir 特殊处理。
沙箱通过 ContextVar 注入，不再通过参数传递。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nanobee.builtin.tool_shell.plugin import ToolShellPlugin
from nanobee.kernel.context_sandbox_var import bind_sandbox, reset_sandbox
from nanobee.kernel.sandbox import ContextSandbox, SandboxError


@pytest.fixture
def plugin(tmp_path: Path) -> ToolShellPlugin:
    """创建 tool_shell 插件实例"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolShellPlugin(workspace=str(workspace), restrict_to_workspace=True)


@pytest.fixture
def user_context_sandbox(tmp_path: Path) -> ContextSandbox:
    """创建用户上下文沙箱"""
    root = tmp_path / "contexts" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    return ContextSandbox(root)


def _with_sandbox(sandbox: ContextSandbox, fn):
    """在 ContextVar 沙箱绑定下执行函数"""
    token = bind_sandbox(sandbox)
    try:
        return fn()
    finally:
        reset_sandbox(token)


# ====== L2 沙箱 ContextVar 注入测试 ======


def test_request_level_sandbox_passing(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox):
    """ContextVar 绑定 sandbox 后，_check_sandbox_path 使用 ContextVar 获取的沙箱"""
    def _test():
        inside_path = str(user_context_sandbox.context_root / "test.sh")
        result = plugin._check_sandbox_path(inside_path)
        assert result is None
    _with_sandbox(user_context_sandbox, _test)


def test_check_sandbox_path_allowed(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox, tmp_path: Path):
    """L2 沙箱校验：路径在沙箱内 → 返回 None（ContextVar 注入）"""
    def _test():
        inside_path = str(user_context_sandbox.context_root / "test.sh")
        result = plugin._check_sandbox_path(inside_path)
        assert result is None
    _with_sandbox(user_context_sandbox, _test)


def test_check_sandbox_path_blocked(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox, tmp_path: Path):
    """L2 沙箱校验：路径在沙箱外 → 返回错误字符串（ContextVar 注入）"""
    def _test():
        outside_path = str(tmp_path / "outside" / "script.sh")
        result = plugin._check_sandbox_path(outside_path)
        assert result is not None
        assert "沙箱拦截" in result
    _with_sandbox(user_context_sandbox, _test)


def test_check_sandbox_path_no_sandbox(plugin: ToolShellPlugin):
    """未绑定 sandbox 时，_check_sandbox_path 返回 None"""
    result = plugin._check_sandbox_path("/tmp/test.sh")
    assert result is None


# ====== working_dir 特殊处理测试 ======


def test_prepare_command_with_working_dir_inside_sandbox(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """working_dir 在沙箱内 → 命令准备成功（ContextVar 注入）"""
    def _test():
        # 沙箱根目录即工作目录
        inside_wd = str(user_context_sandbox.context_root / "test_wd")
        inside_wd_path = Path(inside_wd)
        inside_wd_path.mkdir(parents=True, exist_ok=True)

        # 使用无 restrict_to_workspace 的 plugin 实例进行测试
        no_restrict_plugin = ToolShellPlugin(workspace=str(inside_wd_path), restrict_to_workspace=False)
        result = no_restrict_plugin._prepare_command("echo hello", working_dir=inside_wd)
        assert hasattr(result, "command")  # _PreparedCommand
    _with_sandbox(user_context_sandbox, _test)


def test_prepare_command_with_working_dir_outside_sandbox(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """working_dir 在沙箱外 → L2 拦截（ContextVar 注入）"""
    def _test():
        outside_wd = str(tmp_path / "outside")
        outside_wd_path = Path(outside_wd)
        outside_wd_path.mkdir(parents=True, exist_ok=True)

        # 使用无 restrict_to_workspace 的 plugin 实例，仅测试 L2 沙箱拦截
        no_restrict_plugin = ToolShellPlugin(workspace=str(outside_wd_path), restrict_to_workspace=False)
        result = no_restrict_plugin._prepare_command("echo hello", working_dir=outside_wd)
        assert isinstance(result, str)
        assert "沙箱拦截" in result
    _with_sandbox(user_context_sandbox, _test)


def test_prepare_command_with_working_dir_fallback_to_workspace(
    plugin: ToolShellPlugin,
    tmp_path: Path,
):
    """未提供 working_dir 时，回退到 _workspace"""
    result = plugin._prepare_command("echo hello")
    # 应该使用 plugin._workspace 作为 cwd
    assert hasattr(result, "cwd")
    assert result.cwd == plugin._workspace


# ====== 集成测试：ContextVar 沙箱注入 + _prepare_command ======


def test_request_level_sandbox_workflow(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """完整 ContextVar 注入流程：bind_sandbox → _prepare_command 校验"""
    def _test():
        # 1. 创建沙箱内的工作目录
        inside_wd = user_context_sandbox.context_root / "test_wd"
        inside_wd.mkdir(parents=True, exist_ok=True)

        # 2. 使用无 restrict_to_workspace 的 plugin 实例
        no_restrict_plugin = ToolShellPlugin(workspace=str(inside_wd), restrict_to_workspace=False)

        # 3. 调用 _prepare_command，沙箱通过 ContextVar 获取
        result = no_restrict_plugin._prepare_command("echo hello", working_dir=str(inside_wd))
        assert hasattr(result, "command")
    _with_sandbox(user_context_sandbox, _test)


def test_workspace_boundary_still_works(
    plugin: ToolShellPlugin,
    tmp_path: Path,
):
    """restrict_to_workspace 仍然生效"""
    workspace = Path(plugin._workspace)
    outside_wd = str(tmp_path / "outside_workspace")
    outside_wd_path = Path(outside_wd)
    outside_wd_path.mkdir(parents=True, exist_ok=True)

    result = plugin._prepare_command("echo hello", working_dir=outside_wd)
    assert isinstance(result, str)
    assert "超出配置的工作区" in result


# ====== L1 沙箱 working_dir 特殊处理测试 ======


def test_sanitize_params_working_dir_only_resolves(
    tmp_path: Path,
):
    """working_dir 在 sanitize_params 中只解析，不拦截"""
    root = tmp_path / "contexts" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    # working_dir 指向沙箱外 → 只解析，不抛异常
    outside_wd = str(tmp_path / "outside")
    result = sandbox.sanitize_params("execute_shell", {"working_dir": outside_wd})
    assert "working_dir" in result
    assert Path(result["working_dir"]).resolve() == Path(outside_wd).resolve()


def test_sanitize_params_working_dir_inside_sandbox(
    tmp_path: Path,
):
    """working_dir 在沙箱内 → 解析为绝对路径"""
    root = tmp_path / "contexts" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    inside_wd = root / "sub_wd"
    inside_wd.mkdir(parents=True, exist_ok=True)

    result = sandbox.sanitize_params("execute_shell", {"working_dir": str(inside_wd)})
    assert result["working_dir"] == str(inside_wd.resolve())


def test_sanitize_params_path_field_still_intercepts(
    tmp_path: Path,
):
    """path 字段仍然被严格拦截"""
    root = tmp_path / "contexts" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    outside_path = str(tmp_path / "outside" / "file.txt")
    with pytest.raises(SandboxError):
        sandbox.sanitize_params("read_file", {"path": outside_path})


# ====== 异步执行测试 ======


@pytest.mark.asyncio
async def test_execute_shell_with_request_level_sandbox(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """execute_shell 使用 ContextVar 注入的 sandbox，working_dir 被正确校验"""
    # 创建沙箱内的工作目录
    inside_wd = user_context_sandbox.context_root / "test_wd"
    inside_wd.mkdir(parents=True, exist_ok=True)

    # 使用无 restrict_to_workspace 的 plugin 实例
    no_restrict_plugin = ToolShellPlugin(workspace=str(inside_wd), restrict_to_workspace=False)

    # 绑定沙箱到 ContextVar
    token = bind_sandbox(user_context_sandbox)
    try:
        result = await no_restrict_plugin.execute_tool("execute_shell", command="echo hello", working_dir=str(inside_wd))
    finally:
        reset_sandbox(token)
    assert "hello" in result or "退出码" in result


@pytest.mark.asyncio
async def test_execute_shell_outside_sandbox_fails(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """execute_shell 使用沙箱外的 working_dir → 返回错误（ContextVar 注入）"""
    outside_wd = str(tmp_path / "outside")
    Path(outside_wd).mkdir(parents=True, exist_ok=True)

    # 使用无 restrict_to_workspace 的 plugin 实例，仅测试 L2 沙箱拦截
    no_restrict_plugin = ToolShellPlugin(workspace=str(outside_wd), restrict_to_workspace=False)

    token = bind_sandbox(user_context_sandbox)
    try:
        result = await no_restrict_plugin.execute_tool("execute_shell", command="echo hello", working_dir=outside_wd)
    finally:
        reset_sandbox(token)
    assert isinstance(result, str)
    assert "沙箱拦截" in result


# ====== 边界情况 ======


def test_check_sandbox_path_with_symlink_to_outside(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """符号链接指向沙箱外 → L2 拦截（ContextVar 注入）"""
    def _test():
        # 创建指向外部的符号链接
        outside_file = tmp_path / "outside_file.sh"
        outside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = user_context_sandbox.context_root / "evil_link"
        symlink.symlink_to(outside_file)

        result = plugin._check_sandbox_path(str(symlink))
        assert result is not None
        assert "沙箱拦截" in result
    _with_sandbox(user_context_sandbox, _test)


def test_check_sandbox_path_with_symlink_to_inside(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """符号链接指向沙箱内 → 通过（ContextVar 注入）"""
    def _test():
        # 创建指向内部的符号链接
        inside_file = user_context_sandbox.context_root / "safe.sh"
        inside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = tmp_path / "outside_link"
        symlink.symlink_to(inside_file)

        # 符号链接在外部但指向内部 → 解析后在沙箱内，应通过
        result = plugin._check_sandbox_path(str(symlink))
        assert result is None
    _with_sandbox(user_context_sandbox, _test)
