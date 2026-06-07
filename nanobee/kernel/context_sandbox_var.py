"""
上下文沙箱 ContextVar — 用 ContextVar 替代方法参数透传沙箱实例

遵循 nanobot/security/workspace_access.py 的设计模式，
在异步任务边界通过 ContextVar 注入沙箱，消除逐层传递 _sandbox 参数。

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobee.kernel.sandbox import ContextSandbox

_CURRENT_SANDBOX: ContextVar["ContextSandbox | None"] = ContextVar(
    "nanobee_sandbox",
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


__all__ = [
    "bind_sandbox",
    "current_sandbox",
    "reset_sandbox",
]
