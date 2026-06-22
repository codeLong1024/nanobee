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
from nanobee.kernel.context_sandbox_var import (
    bind_process_workspace,
    bind_sandbox,
    reset_process_workspace,
    reset_sandbox,
)
from nanobee.kernel.sandbox import ContextSandbox
from nanobee.exceptions import SandboxViolationError


@pytest.fixture
def plugin(tmp_path: Path) -> ToolShellPlugin:
    """创建 tool_shell 插件实例"""
    return ToolShellPlugin()


@pytest.fixture
def user_context_sandbox(tmp_path: Path) -> ContextSandbox:
    """创建用户上下文沙箱"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    return ContextSandbox(root)


def _with_sandbox(sandbox: ContextSandbox, fn):
    """在 ContextVar 沙箱绑定下执行函数"""
    token = bind_sandbox(sandbox)
    try:
        return fn()
    finally:
        reset_sandbox(token)


# ====== _resolve_and_validate_working_dir 统一入口测试 ======


def test_resolve_and_validate_sandbox_allowed(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox):
    """路径在沙箱内 → 返回 (path, None)"""
    def _test():
        inside_path = user_context_sandbox.context_root / "test.sh"
        resolved, error = plugin._resolve_and_validate_working_dir(str(inside_path))
        assert error is None
        assert str(Path(resolved).resolve()) == str(inside_path.resolve())
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_sandbox_blocked(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox, tmp_path: Path):
    """路径在沙箱外 → 返回 ("", error)"""
    def _test():
        outside_path = str(tmp_path / "outside" / "script.sh")
        resolved, error = plugin._resolve_and_validate_working_dir(outside_path)
        assert resolved == ""
        assert error is not None
        assert "沙箱拦截" in error
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_no_sandbox_no_process_ws(plugin: ToolShellPlugin):
    """无 sandbox 且无 process_workspace → 显式 working_dir 仍然可解析"""
    resolved, error = plugin._resolve_and_validate_working_dir("/tmp/test.sh")
    assert error is None
    assert str(Path(resolved).resolve()) == "/tmp/test.sh"


def test_resolve_and_validate_no_working_dir_no_process_ws(plugin: ToolShellPlugin):
    """无 working_dir 且无 process_workspace → 返回错误"""
    resolved, error = plugin._resolve_and_validate_working_dir(None)
    assert resolved == ""
    assert error is not None
    assert "未设置 process_workspace" in error


def test_resolve_and_validate_symlink_to_outside_blocked(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """符号链接指向沙箱外 → L2 拦截"""
    def _test():
        outside_file = tmp_path / "outside_file.sh"
        outside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = user_context_sandbox.context_root / "evil_link"
        symlink.symlink_to(outside_file)
        resolved, error = plugin._resolve_and_validate_working_dir(str(symlink))
        assert resolved == ""
        assert error is not None
        assert "沙箱拦截" in error
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_symlink_to_inside_allowed(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """符号链接指向沙箱内 → 通过"""
    def _test():
        inside_file = user_context_sandbox.context_root / "safe.sh"
        inside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = tmp_path / "outside_link"
        symlink.symlink_to(inside_file)
        resolved, error = plugin._resolve_and_validate_working_dir(str(symlink))
        assert error is None
        assert inside_file.resolve() in Path(resolved).resolve().parents or Path(resolved).resolve() == inside_file.resolve()
    _with_sandbox(user_context_sandbox, _test)


# ====== working_dir 特殊处理测试 ======


def test_prepare_command_with_working_dir_inside_sandbox(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """working_dir 在沙箱内 → 命令准备成功（ContextVar 注入）"""
    def _test():
        inside_wd = str(user_context_sandbox.context_root / "test_wd")
        inside_wd_path = Path(inside_wd)
        inside_wd_path.mkdir(parents=True, exist_ok=True)

        result = plugin._prepare_command("echo hello", working_dir=inside_wd)
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
        Path(outside_wd).mkdir(parents=True, exist_ok=True)

        result = plugin._prepare_command("echo hello", working_dir=outside_wd)
        assert isinstance(result, str)
        assert "沙箱拦截" in result
    _with_sandbox(user_context_sandbox, _test)


def test_prepare_command_without_working_dir_no_process_ws(
    plugin: ToolShellPlugin,
):
    """未提供 working_dir 且无 process_workspace → 返回错误"""
    result = plugin._prepare_command("echo hello")
    assert isinstance(result, str)
    assert "未设置 process_workspace" in result


def test_prepare_command_fallback_to_process_workspace(
    plugin: ToolShellPlugin,
    tmp_path: Path,
):
    """未提供 working_dir 时，优先使用 process_workspace（bwrap 可写区）"""
    process_ws = tmp_path / "process_ws"
    process_ws.mkdir(parents=True, exist_ok=True)

    token = bind_process_workspace(process_ws)
    try:
        result = plugin._prepare_command("echo hello")
        assert hasattr(result, "cwd")
        assert result.cwd == str(process_ws)
    finally:
        reset_process_workspace(token)


# ====== 集成测试：ContextVar 沙箱注入 + _prepare_command ======


def test_request_level_sandbox_workflow(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """完整 ContextVar 注入流程：bind_sandbox → _prepare_command 校验"""
    def _test():
        inside_wd = user_context_sandbox.context_root / "test_wd"
        inside_wd.mkdir(parents=True, exist_ok=True)

        result = plugin._prepare_command("echo hello", working_dir=str(inside_wd))
        assert hasattr(result, "command")
    _with_sandbox(user_context_sandbox, _test)


def test_working_dir_outside_process_workspace_blocked(
    plugin: ToolShellPlugin,
    tmp_path: Path,
):
    """working_dir 在 process_workspace 外 → 被无条件拦截"""
    process_ws = tmp_path / "process_ws"
    process_ws.mkdir(parents=True, exist_ok=True)
    outside_wd = tmp_path / "outside"
    outside_wd.mkdir(parents=True, exist_ok=True)

    token = bind_process_workspace(process_ws)
    try:
        result = plugin._prepare_command("echo hello", working_dir=str(outside_wd))
    finally:
        reset_process_workspace(token)

    assert isinstance(result, str)
    assert "超出可写工作目录" in result


# ====== L1 沙箱 working_dir 特殊处理测试 ======


def test_sanitize_params_working_dir_only_resolves(
    tmp_path: Path,
):
    """working_dir 在 sanitize_params 中只解析，不拦截"""
    root = tmp_path / "users" / "user-a"
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
    root = tmp_path / "users" / "user-a"
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
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    outside_path = str(tmp_path / "outside" / "file.txt")
    with pytest.raises(SandboxViolationError):
        sandbox.sanitize_params("read_file", {"path": outside_path})


# ====== 异步执行测试 ======


@pytest.mark.asyncio
async def test_execute_shell_with_request_level_sandbox(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """execute_shell 使用 ContextVar 注入的 sandbox，working_dir 被正确校验"""
    inside_wd = user_context_sandbox.context_root / "test_wd"
    inside_wd.mkdir(parents=True, exist_ok=True)

    token = bind_sandbox(user_context_sandbox)
    try:
        result = await plugin.execute_tool("execute_shell", command="echo hello", working_dir=str(inside_wd))
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

    token = bind_sandbox(user_context_sandbox)
    try:
        result = await plugin.execute_tool("execute_shell", command="echo hello", working_dir=outside_wd)
    finally:
        reset_sandbox(token)
    assert isinstance(result, str)
    assert "沙箱拦截" in result


# ====== 边界情况 ======


def test_resolve_and_validate_invalid_path(plugin: ToolShellPlugin):
    """无效路径 → 返回错误"""
    resolved, error = plugin._resolve_and_validate_working_dir("\x00invalid")
    assert resolved == ""
    assert error is not None
    assert "无法解析" in error


# ==============================================================================
# 进程级沙箱包裹测试
# ==============================================================================


def test_wrap_sandbox_no_config(plugin: ToolShellPlugin):
    """未配置 sandbox 后端时，命令原样返回"""
    result = plugin._wrap_sandbox("echo hello", "/tmp")
    assert result == "echo hello"


def test_wrap_sandbox_empty_config(plugin: ToolShellPlugin):
    """空字符串配置也返回原命令"""
    plugin.get_config = lambda k, d="": ""  # type: ignore[method-assign]
    result = plugin._wrap_sandbox("echo hello", "/tmp")
    assert result == "echo hello"


def test_wrap_sandbox_bwrap_not_installed(plugin: ToolShellPlugin, tmp_path: Path):
    """bwrap 未安装时优雅降级"""
    import shutil
    if shutil.which("bwrap"):
        pytest.skip("bwrap 已安装，跳过降级测试")
    plugin.get_config = lambda k, d="": "bwrap"  # type: ignore[method-assign]
    result = plugin._wrap_sandbox("echo hello", str(tmp_path))
    # 应该返回原始命令（不支持跳过不报错）
    assert result == "echo hello"


def test_wrap_sandbox_unknown_backend(plugin: ToolShellPlugin, tmp_path: Path):
    """未知后端时优雅降级"""
    plugin.get_config = lambda k, d="": "nonexistent"  # type: ignore[method-assign]
    result = plugin._wrap_sandbox("echo hello", str(tmp_path))
    assert result == "echo hello"


# ==============================================================================
# sandbox.py 模块直接测试
# ==============================================================================


from nanobee.builtin.tool_shell.sandbox import wrap_command, _BACKENDS


def test_backends_available():
    """后端的依赖检查返回结果"""
    for name, (backend_fn, dep_check) in _BACKENDS.items():
        available, error_msg = dep_check()
        # 如果 bwrap 未安装，error_msg 应包含提示信息
        if not available:
            assert error_msg is not None
            assert "未安装" in error_msg or "install" in error_msg


def test_wrap_command_unknown_backend_raises():
    """未知后端名抛 ValueError"""
    import pytest
    with pytest.raises(ValueError, match="未知的沙箱后端"):
        wrap_command("ghost", "echo hi", "/tmp", "/tmp")


def test_wrap_command_missing_dependency_raises(tmp_path: Path):
    """bwrap 未安装时抛 RuntimeError"""
    import shutil
    import pytest
    if shutil.which("bwrap") is None:
        with pytest.raises(RuntimeError, match="bwrap 未安装"):
            wrap_command("bwrap", "echo hi", str(tmp_path), str(tmp_path))


@pytest.mark.skipif(
    __import__("shutil").which("bwrap") is None,
    reason="bwrap 未安装",
)
def test_bwrap_wraps_command(tmp_path: Path):
    """bwrap 存在时正确包裹命令"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command("bwrap", "echo hello", str(ws), str(ws))
    assert wrapped.startswith("bwrap ")
    assert "--new-session" in wrapped
    assert "--die-with-parent" in wrapped
    assert f"--bind {ws}" in wrapped or f"--bind {ws}/" in wrapped
    assert "echo hello" in wrapped
    assert "--chdir" in wrapped


def test_bwrap_cwd_outside_workspace_raises(tmp_path: Path):
    """cwd 不在 workspace 内 → ValueError 而非静默回退"""
    import pytest
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside_cwd = tmp_path / "sibling_dir"
    outside_cwd.mkdir()
    with pytest.raises(ValueError, match="沙箱工作目录不在 workspace 内"):
        wrap_command("bwrap", "echo hi", str(ws), str(outside_cwd))


@pytest.mark.skipif(
    __import__("shutil").which("bwrap") is None,
    reason="bwrap 未安装",
)
def test_bwrap_actually_isolates(tmp_path: Path):
    """bwrap 容器内无法访问父目录外的文件（需要 rootless bwrap）"""
    import asyncio

    ws = tmp_path / "sandbox_ws"
    ws.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("confidential", encoding="utf-8")

    wrapped_cmd = wrap_command(
        "bwrap",
        f"cat /proc/self/root{secret} 2>&1 || echo 'BLOCKED'",
        str(ws), str(ws),
    )

    async def _run():
        proc = await asyncio.create_subprocess_shell(
            wrapped_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode() + stderr.decode()

    output = asyncio.run(_run())
    # 应该无法读取 secret.txt（被 tmpfs 遮掩）
    assert "confidential" not in output
