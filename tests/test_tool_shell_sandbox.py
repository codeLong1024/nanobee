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
from nanobee.plugins.base import PluginMetadata


@pytest.fixture
def plugin(tmp_path: Path) -> ToolShellPlugin:
    """创建 tool_shell 插件实例"""
    return ToolShellPlugin(PluginMetadata(name="tool_shell", plugin_type="tool"))


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
    """显式 working_dir 直通（框架校验，插件不做解析）"""
    def _test():
        inside_path = user_context_sandbox.context_root / "test.sh"
        resolved, error = plugin._resolve_and_validate_working_dir(str(inside_path))
        assert error is None
        assert resolved == str(inside_path)
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_sandbox_blocked(plugin: ToolShellPlugin, user_context_sandbox: ContextSandbox, tmp_path: Path):
    """显式 working_dir 直通（边界校验由框架 sanitize_params 负责）"""
    def _test():
        outside_path = str(tmp_path / "outside" / "script.sh")
        resolved, error = plugin._resolve_and_validate_working_dir(outside_path)
        assert resolved == outside_path
        assert error is None
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_no_sandbox_no_process_ws(plugin: ToolShellPlugin):
    """无 sandbox 且无 process_workspace → 显式 working_dir 直通"""
    resolved, error = plugin._resolve_and_validate_working_dir("/tmp/test.sh")
    assert error is None
    assert resolved == "/tmp/test.sh"


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
    """符号链接指向沙箱外 → 直通（边界校验由框架负责）"""
    def _test():
        outside_file = tmp_path / "outside_file.sh"
        outside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = user_context_sandbox.context_root / "evil_link"
        symlink.symlink_to(outside_file)
        resolved, error = plugin._resolve_and_validate_working_dir(str(symlink))
        assert resolved == str(symlink)
        assert error is None
    _with_sandbox(user_context_sandbox, _test)


def test_resolve_and_validate_symlink_to_inside_allowed(
    plugin: ToolShellPlugin,
    user_context_sandbox: ContextSandbox,
    tmp_path: Path,
):
    """符号链接指向沙箱内 → 直通（插件不解析路径）"""
    def _test():
        inside_file = user_context_sandbox.context_root / "safe.sh"
        inside_file.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        symlink = tmp_path / "outside_link"
        symlink.symlink_to(inside_file)
        resolved, error = plugin._resolve_and_validate_working_dir(str(symlink))
        assert error is None
        assert resolved == str(symlink)
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
    """working_dir 在沙箱外 → 直通（边界校验由框架 sanitize_params 负责）"""
    def _test():
        outside_wd = str(tmp_path / "outside")
        Path(outside_wd).mkdir(parents=True, exist_ok=True)

        result = plugin._prepare_command("echo hello", working_dir=outside_wd)
        assert hasattr(result, "command")  # 插件不做拦截，正常准备命令
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
    """显式 working_dir 在 process_workspace 外 → 直通（边界校验由框架负责）"""
    process_ws = tmp_path / "process_ws"
    process_ws.mkdir(parents=True, exist_ok=True)
    outside_wd = tmp_path / "outside"
    outside_wd.mkdir(parents=True, exist_ok=True)

    token = bind_process_workspace(process_ws)
    try:
        result = plugin._prepare_command("echo hello", working_dir=str(outside_wd))
    finally:
        reset_process_workspace(token)

    assert hasattr(result, "command")


# ====== L1 沙箱 working_dir schema 驱动测试 ======


def test_sanitize_params_working_dir_only_resolves(
    tmp_path: Path,
):
    """无 x-constraint 时 working_dir 直通（sanitize_params 不猜测语义）"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    outside_wd = str(tmp_path / "outside")
    # 无 schema → working_dir 直通，不做任何处理
    result = sandbox.sanitize_params("execute_shell", {"working_dir": outside_wd})
    assert result["working_dir"] == outside_wd


def test_sanitize_params_working_dir_inside_sandbox(
    tmp_path: Path,
):
    """x-constraint: workspace → 在 process_workspace 内解析"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root, process_workspace=ws)

    inside_wd = ws / "sub_wd"
    inside_wd.mkdir(parents=True, exist_ok=True)

    schema = {"properties": {"working_dir": {"x-constraint": "workspace"}}}
    result = sandbox.sanitize_params("execute_shell", {"working_dir": str(inside_wd)}, param_schema=schema)
    assert result["working_dir"] == str(inside_wd.resolve())


def test_sanitize_params_path_field_still_intercepts(
    tmp_path: Path,
):
    """x-constraint: sandbox → 路径被拦截"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root)

    outside_path = str(tmp_path / "outside" / "file.txt")
    schema = {"properties": {"path": {"x-constraint": "sandbox"}}}
    with pytest.raises(SandboxViolationError):
        sandbox.sanitize_params("read_file", {"path": outside_path}, param_schema=schema)


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
    """execute_shell 显式 working_dir 直通（边界校验由框架 sanitize_params 负责）"""
    outside_wd = str(tmp_path / "outside")
    Path(outside_wd).mkdir(parents=True, exist_ok=True)

    token = bind_sandbox(user_context_sandbox)
    try:
        result = await plugin.execute_tool("execute_shell", command="echo hello", working_dir=outside_wd)
    finally:
        reset_sandbox(token)
    # 插件不拦截，正常执行（bwrap 硬件隔离兜底）
    assert "hello" in result or "退出码" in result


# ====== 边界情况 ======


def test_resolve_and_validate_invalid_path(plugin: ToolShellPlugin):
    """显式 working_dir 直通（不做路径解析）"""
    resolved, error = plugin._resolve_and_validate_working_dir("\x00invalid")
    assert resolved == "\x00invalid"
    assert error is None


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


# ====== extra_env / --setenv 测试 ======


def test_bwrap_extra_env_adds_setenv(tmp_path: Path):
    """extra_env={"KEY": "val"} → bwrap 参数包含 --setenv KEY val。
    值作为 bwrap 命令行参数存在于返回字符串中（ps 可见），本机制仅保证
    密钥不进入 LLM 视角的 command 字段。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo hello", str(ws), str(ws),
        extra_env={"TIANYANCHA_TOKEN": "sk-test123"},
    )
    assert "--setenv" in wrapped
    assert "TIANYANCHA_TOKEN" in wrapped
    assert "sk-test123" in wrapped
    # --setenv 必须在 -- 之前（bwrap 语法要求）
    dash_dash_idx = wrapped.index(" -- ")
    setenv_idx = wrapped.index("--setenv")
    assert setenv_idx < dash_dash_idx
    # LLM 原始 command 不含密钥（密钥仅存在于 -- 之前的 bwrap 参数段）
    _cmd_after_dd = wrapped.split(" -- ", 1)[1]
    assert "sk-test123" not in _cmd_after_dd


def test_bwrap_extra_env_none_no_setenv(tmp_path: Path):
    """extra_env=None → 不添加 --setenv"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo hello", str(ws), str(ws),
        extra_env=None,
    )
    assert "--setenv" not in wrapped


def test_bwrap_extra_env_empty_no_setenv(tmp_path: Path):
    """extra_env={} → 不添加 --setenv"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo hello", str(ws), str(ws),
        extra_env={},
    )
    assert "--setenv" not in wrapped


def test_bwrap_extra_env_multiple_keys(tmp_path: Path):
    """多条 extra_env → 多条 --setenv"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo hello", str(ws), str(ws),
        extra_env={"TOKEN_A": "val_a", "TOKEN_B": "val_b"},
    )
    assert wrapped.count("--setenv") == 2
    assert "TOKEN_A" in wrapped
    assert "val_a" in wrapped
    assert "TOKEN_B" in wrapped
    assert "val_b" in wrapped


def test_bwrap_extra_env_preserves_other_args(tmp_path: Path):
    """extra_env 不影响其他 bwrap 参数"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo hello", str(ws), str(ws),
        extra_env={"KEY": "val"},
    )
    assert wrapped.startswith("bwrap ")
    assert "--new-session" in wrapped
    assert "--die-with-parent" in wrapped
    assert f"--bind {ws}" in wrapped or f"--bind {ws}/" in wrapped
    assert "echo hello" in wrapped
    assert "--chdir" in wrapped


def test_wrap_command_passes_extra_env_to_backend(tmp_path: Path):
    """wrap_command 将 extra_env 正确传递给后端函数"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    wrapped = wrap_command(
        "bwrap", "echo test", str(ws), str(ws),
        extra_env={"SECRET_KEY": "secret_value"},
    )
    # 值作为 bwrap --setenv 参数出现在命令字符串中（ps 可见）
    assert "SECRET_KEY" in wrapped
    assert "secret_value" in wrapped


# ====== plugin.py secrets 配置测试 ======


def test_secrets_config_injected_as_extra_env(plugin: ToolShellPlugin, tmp_path: Path):
    """plugins.tool_shell.secrets 配置 → extra_env 传入 wrap_command"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    ws_inner = ws / "sub"
    ws_inner.mkdir()

    with patch.object(
        plugin, "get_config",
        side_effect=lambda key, default=None: {
            "sandbox": "bwrap",
            "env": {},
            "secrets": {"TIANYANCHA_TOKEN": "sk-mock-token"},
            "extra_mounts": [],
        }.get(key, default),
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_process_workspace",
        return_value=ws,
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_ro_bind",
        return_value=[],
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_rw_bind",
        return_value=[],
    ):
        wrapped = plugin._wrap_sandbox("echo hello", str(ws))
        # secrets 通过 --setenv 注入（值在 bwrap 参数中，ps 可见，但不进入 LLM command）
        assert "export " not in wrapped
        assert "--setenv" in wrapped
        assert "TIANYANCHA_TOKEN" in wrapped


def test_env_config_uses_setenv_not_export(plugin: ToolShellPlugin, tmp_path: Path):
    """env 配置通过 --setenv 注入，不使用 export 前缀（方案 A：env + secrets 统一走 --setenv）"""
    ws = tmp_path / "workspace"
    ws.mkdir()

    with patch.object(
        plugin, "get_config",
        side_effect=lambda key, default=None: {
            "sandbox": "bwrap",
            "env": {"PYTHONPATH": "/custom/path"},
            "secrets": {},
            "extra_mounts": [],
        }.get(key, default),
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_process_workspace",
        return_value=ws,
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_ro_bind",
        return_value=[],
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_rw_bind",
        return_value=[],
    ):
        wrapped = plugin._wrap_sandbox("echo hello", str(ws))
        # env 值应通过 --setenv 注入
        assert "--setenv PYTHONPATH" in wrapped
        assert "/custom/path" in wrapped
        # 不应通过 export 前缀注入
        assert "export PYTHONPATH" not in wrapped


def test_env_excludes_secrets_keys(plugin: ToolShellPlugin, tmp_path: Path):
    """env 和 secrets 有同名 key 时，secrets 值优先，全部通过 --setenv 注入"""
    ws = tmp_path / "workspace"
    ws.mkdir()

    with patch.object(
        plugin, "get_config",
        side_effect=lambda key, default=None: {
            "sandbox": "bwrap",
            "env": {"TIANYANCHA_TOKEN": "visible-in-export", "PYTHONPATH": "/x"},
            "secrets": {"TIANYANCHA_TOKEN": "sk-safe-token"},
            "extra_mounts": [],
        }.get(key, default),
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_process_workspace",
        return_value=ws,
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_ro_bind",
        return_value=[],
    ), patch(
        "nanobee.builtin.tool_shell.plugin.current_bwrap_rw_bind",
        return_value=[],
    ):
        wrapped = plugin._wrap_sandbox("echo hello", str(ws))
        # 不应有任何 export 前缀
        assert "export " not in wrapped
        # TIANYANCHA_TOKEN 使用 secrets 值，不用 env 值
        assert "sk-safe-token" in wrapped
        assert "visible-in-export" not in wrapped
        # PYTHONPATH 也通过 --setenv 注入
        assert "--setenv PYTHONPATH" in wrapped
        assert "--setenv TIANYANCHA_TOKEN" in wrapped


def test_secrets_no_sandbox_not_injected(plugin: ToolShellPlugin):
    """未配置 sandbox 时，secrets 不会注入（命令不经过 bwrap 包裹）"""
    with patch.object(
        plugin, "get_config",
        side_effect=lambda key, default=None: {
            "sandbox": "",
            "secrets": {"TOKEN": "sk-test"},
        }.get(key, default),
    ):
        # 传入的必须是绝对路径，插件直接使用不做重解析
        result = plugin._wrap_sandbox("echo hello", "/tmp")
        assert result == "echo hello"
        assert "TOKEN" not in result


def test_env_path_prefix_appends_system_paths(plugin: ToolShellPlugin):
    """env PATH 作为前缀，框架自动追加最小系统路径确保 execvp 能搜索 sh"""
    with patch.object(
        plugin, "get_config",
        side_effect=lambda key, default=None: {
            "sandbox": "bwrap",
            "env": {"PATH": "/custom/bin"},
            "secrets": {},
        }.get(key, default),
    ), patch("nanobee.builtin.tool_shell.plugin.current_process_workspace",
            return_value=Path("/tmp/ws")):
        result = plugin._wrap_sandbox("echo hello", "/tmp/ws")
        # --setenv PATH 的值应包含用户前缀 + 系统路径
        setenv_idx = result.index("--setenv")
        after_setenv = result[setenv_idx:]
        assert "/custom/bin:/usr/local/bin:/usr/bin:/bin" in after_setenv
        # 不应包含字面量 $PATH
        assert "$PATH" not in result
