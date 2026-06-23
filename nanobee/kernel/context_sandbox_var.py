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
from dataclasses import dataclass
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


# bwrap 额外只读挂载路径列表 —— 部署方通过 skills.enabled 声明后，
# 框架自动推导 enabled 实例技能目录为 bwrap --ro-bind-try 目标。
# tool_shell 插件在 _wrap_sandbox 中消费此 ContextVar。
_CURRENT_BWRAP_RO_BIND: ContextVar[list[str] | None] = ContextVar(
    "nanobee_bwrap_ro_bind",
    default=None,
)


def bind_bwrap_ro_bind(paths: list[str]) -> Token[list[str] | None]:
    """在当前异步任务中绑定 bwrap 额外只读挂载路径。

    Args:
        paths: 只读挂载路径列表（绝对路径字符串）

    Returns:
        Token 用于后续 reset_bwrap_ro_bind 恢复
    """
    return _CURRENT_BWRAP_RO_BIND.set(paths)


def reset_bwrap_ro_bind(token: Token[list[str] | None]) -> None:
    """恢复绑定的 bwrap ro-bind 到绑定前的状态。

    Args:
        token: bind_bwrap_ro_bind 返回的 Token
    """
    _CURRENT_BWRAP_RO_BIND.reset(token)


def current_bwrap_ro_bind() -> list[str] | None:
    """获取当前异步任务中绑定的 bwrap 额外只读挂载路径。

    tool_shell 插件通过此 ContextVar 获取启用的实例技能目录，
    追加为 bwrap --ro-bind-try 挂载点。

    Returns:
        只读挂载路径列表，未绑定时返回 None
    """
    return _CURRENT_BWRAP_RO_BIND.get()


# ── Per-turn 路由上下文（对齐 nanobot 的 RequestContext 概念） ──────────────────
#
# 每个 Agent turn 开始时绑定一个 RequestContext，包含该 turn 的所有路由信息：
#   - channel:  来源通道名（如 channel_dingtalk）
#   - chat_id:  通道内会话标识（如用户 staff_id）
#   - context_id: 用户上下文 ID（用于目录隔离）
#   - session_id: 会话持久化 ID（用于 SessionManager 存储）
#
# 所有 ContextAware 工具通过 current_request_context() 一次性获取完整路由信息，
# 避免散落的单个 ContextVar 导致字段遗漏（如子代理结果注入丢失 session_id）。
#
# 对齐 nanobot 的设计：RequestContext 统一注入，SpawnTool 通过 set_context() 接收。


@dataclass(frozen=True)
class RequestContext:
    """Per-turn 路由上下文，对齐 nanobot 的 RequestContext 概念。"""

    channel: str      # 来源通道名（如 channel_dingtalk）
    chat_id: str      # 通道内会话标识（如用户 staff_id）
    context_id: str   # 用户上下文 ID，用于目录隔离和结果路由
    session_id: str   # 会话持久化 ID，用于 SessionManager 存储


_CURRENT_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "nanobee_request_context",
    default=None,
)


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    """在当前异步任务中绑定 per-turn 路由上下文。

    Args:
        ctx: RequestContext 实例，包含 channel/chat_id/context_id/session_id

    Returns:
        Token 用于后续 reset_request_context 恢复
    """
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """恢复绑定的路由上下文到绑定前的状态。

    Args:
        token: bind_request_context 返回的 Token
    """
    _CURRENT_REQUEST_CONTEXT.reset(token)


def current_request_context() -> RequestContext | None:
    """获取当前异步任务中绑定的 per-turn 路由上下文。

    Returns:
        RequestContext 实例，未绑定时返回 None
    """
    return _CURRENT_REQUEST_CONTEXT.get()


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
    "bind_bwrap_ro_bind",
    "current_bwrap_ro_bind",
    "reset_bwrap_ro_bind",
    "RequestContext",
    "bind_request_context",
    "current_request_context",
    "reset_request_context",
]
