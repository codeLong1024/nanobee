"""
上下文沙箱 ContextVar — 用 ContextVar 替代方法参数透传沙箱实例

遵循 nanobot/security/workspace_access.py 的设计模式，
在异步任务边界通过 ContextVar 注入沙箱，消除逐层传递 _sandbox 参数。

同时还管理 per-request 的 tmp 路径 ContextVar，
让插件通过 self.tmp 动态获取当前用户上下文的临时目录。

使用方式：
    from nanobee.kernel.context_sandbox_var import bind_sandbox, current_sandbox

    # 在 Agent 轮次开始时绑定
    sandbox = ContextSandbox(context_root)
    token = bind_sandbox(sandbox)
    try:
        # 协程中任意深度调用 current_sandbox() 均可获取
        ...
    finally:
        reset_sandbox(token)
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobee.kernel.sandbox import ContextSandbox

_CURRENT_SANDBOX: ContextVar["ContextSandbox | None"] = ContextVar(
    "nanobee_sandbox",
    default=None,
)

# 当前请求的 tmp 根目录（per-user: ~/.nanobee/users/<user>/.tmp/）
# 插件访问 self.tmp 时会自动加上插件名作为子目录
_CURRENT_TMP: ContextVar[Path | None] = ContextVar(
    "nanobee_tmp",
    default=None,
)

# 当前请求的用户上下文根目录（per-user: ~/.nanobee/users/<user>/）
# 框架只提供 basedir，插件自己创建子目录
_CURRENT_CONTEXT_ROOT: ContextVar[Path | None] = ContextVar(
    "nanobee_context_root",
    default=None,
)


def bind_sandbox(sandbox: ContextSandbox) -> Token[ContextSandbox | None]:
    """在当前异步任务中绑定沙箱实例。

    Args:
        sandbox: ContextSandbox 实例

    Returns:
        Token 用于后续 reset_sandbox 恢复
    """
    return _CURRENT_SANDBOX.set(sandbox)


def reset_sandbox(token: Token[ContextSandbox | None]) -> None:
    """恢复绑定的沙箱到绑定前的状态。

    Args:
        token: bind_sandbox 返回的 Token
    """
    _CURRENT_SANDBOX.reset(token)


def current_sandbox() -> ContextSandbox | None:
    """获取当前异步任务中绑定的沙箱实例。

    Returns:
        ContextSandbox 实例，未绑定时返回 None
    """
    return _CURRENT_SANDBOX.get()


def bind_tmp(tmp_dir: Path) -> Token[Path | None]:
    """在当前异步任务中绑定 tmp 根目录。

    Args:
        tmp_dir: per-user 的 tmp 根目录

    Returns:
        Token 用于后续 reset_tmp 恢复
    """
    return _CURRENT_TMP.set(tmp_dir)


def reset_tmp(token: Token[Path | None]) -> None:
    """恢复绑定的 tmp 到绑定前的状态。

    Args:
        token: bind_tmp 返回的 Token
    """
    _CURRENT_TMP.reset(token)


def current_tmp() -> Path | None:
    """获取当前异步任务中绑定的 tmp 根目录。

    插件通过 self.tmp 属性访问（自动追加插件名），
    无需直接调用此函数。

    Returns:
        tmp 根目录，未绑定时返回 None
    """
    return _CURRENT_TMP.get()


def bind_context_root(ctx_root: Path) -> Token[Path | None]:
    """在当前异步任务中绑定用户上下文根目录。

    Args:
        ctx_root: 用户上下文根目录（basedir）

    Returns:
        Token 用于后续 reset_context_root 恢复
    """
    return _CURRENT_CONTEXT_ROOT.set(ctx_root)


def reset_context_root(token: Token[Path | None]) -> None:
    """恢复绑定的上下文根目录到绑定前的状态。

    Args:
        token: bind_context_root 返回的 Token
    """
    _CURRENT_CONTEXT_ROOT.reset(token)


def current_context_root() -> Path | None:
    """获取当前异步任务中绑定的用户上下文根目录。

    插件通过 self.context_root 属性访问，
    拿到 basedir 后自己创建持久化子目录。

    Returns:
        用户上下文根目录，未绑定时返回 None
    """
    return _CURRENT_CONTEXT_ROOT.get()


# 当前请求的子进程工作区边界（per-user: ~/.nanobee/users/<user>/workspace/）
# 定义子进程可访问的目录边界，与 ContextRoot（路径校验边界）解耦。
_CURRENT_PROCESS_WORKSPACE: ContextVar[Path | None] = ContextVar(
    "nanobee_process_workspace",
    default=None,
)


def bind_process_workspace(workspace: Path) -> Token[Path | None]:
    """在当前异步任务中绑定子进程工作区边界。

    Args:
        workspace: 子进程可访问的工作区根目录

    Returns:
        Token 用于后续 reset_process_workspace 恢复
    """
    return _CURRENT_PROCESS_WORKSPACE.set(workspace)


def reset_process_workspace(token: Token[Path | None]) -> None:
    """恢复绑定的子进程工作区到绑定前的状态。

    Args:
        token: bind_process_workspace 返回的 Token
    """
    _CURRENT_PROCESS_WORKSPACE.reset(token)


def current_process_workspace() -> Path | None:
    """获取当前异步任务中绑定的子进程工作区边界。

    tool_shell 插件通过此 ContextVar 获取 bwrap workspace 路径，
    遵循框架无知论：工具层只读标记、不决策。

    Returns:
        子进程工作区根目录，未绑定时返回 None
    """
    return _CURRENT_PROCESS_WORKSPACE.get()


__all__ = [
    "bind_sandbox",
    "current_sandbox",
    "reset_sandbox",
    "bind_tmp",
    "current_tmp",
    "reset_tmp",
    "bind_context_root",
    "current_context_root",
    "reset_context_root",
    "bind_process_workspace",
    "current_process_workspace",
    "reset_process_workspace",
]
