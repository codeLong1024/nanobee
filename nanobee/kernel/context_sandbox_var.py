"""
上下文沙箱 ContextVar — 用 ContextVar 替代方法参数透传沙箱实例

遵循 nanobot/security/workspace_access.py 的设计模式，
在异步任务边界通过 ContextVar 注入沙箱，消除逐层传递 _sandbox 参数。

同时还管理 per-request 的 tmp 路径 ContextVar，
让插件通过 self.tmp 动态获取当前用户上下文的临时目录。

使用方式：
    from nanobee.kernel.context_sandbox_var import (
        CURRENT_SANDBOX as sandbox_slot,
    )

    # 或直接使用模块级别名（推荐）
    from nanobee.kernel.context_sandbox_var import bind_sandbox, current_sandbox

    token = bind_sandbox(sandbox)
    try:
        ...
    finally:
        reset_sandbox(token)
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from nanobee.kernel.sandbox import ContextSandbox

T = TypeVar("T")


class SandboxVar(Generic[T]):
    """ContextVar 泛型包装 —— 替代 6×3 bind/reset/current 函数样板。

    用法：
        slot = SandboxVar[Path | None]("name", default=None)
        token = slot.bind(value)
        slot.reset(token)
        val = slot.current()

    所有 ContextVar slot 声明集中在此文件底部，
    模块级别名保证 ``from ... import bind_xxx`` 等现有导入不受影响。
    """

    def __init__(self, name: str, default: T = None) -> None:  # type: ignore[assignment]
        self._var: ContextVar[T] = ContextVar(name, default=default)

    def bind(self, value: T) -> Token[T]:
        """在当前异步任务中绑定值。

        Args:
            value: 要绑定的值

        Returns:
            Token 用于后续 reset 恢复
        """
        return self._var.set(value)

    def reset(self, token: Token[T]) -> None:
        """恢复上下文到绑定前的状态。

        Args:
            token: bind 返回的 Token
        """
        self._var.reset(token)

    def current(self) -> T:
        """获取当前绑定的值，未绑定时返回 default。"""
        return self._var.get()


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
    metadata: dict = field(default_factory=dict)  # 通道附加元数据


# ── 6 个 slot 声明 ──────────────────────────────────────────────────────────

# 当前请求的沙箱实例（per-task: 由 AgentLoop 在 _state_run 中绑定）
CURRENT_SANDBOX = SandboxVar["ContextSandbox | None"]("nanobee_sandbox", default=None)

# 当前请求的 tmp 根目录（per-user: ~/.nanobee/users/<user>/.tmp/）
# 插件访问 self.tmp 时会自动加上插件名作为子目录
CURRENT_TMP = SandboxVar[Path | None]("nanobee_tmp", default=None)

# 当前请求的用户上下文根目录（per-user: ~/.nanobee/users/<user>/）
# 框架只提供 basedir，插件自己创建子目录
CURRENT_CONTEXT_ROOT = SandboxVar[Path | None]("nanobee_context_root", default=None)

# 当前请求的子进程工作区边界（per-user: ~/.nanobee/users/<user>/workspace/）
# 定义子进程可访问的目录边界，与 ContextRoot（路径校验边界）解耦。
CURRENT_PROCESS_WORKSPACE = SandboxVar[Path | None]("nanobee_process_workspace", default=None)

# bwrap 额外只读挂载路径列表 —— 部署方通过 skills.enabled 声明后，
# 框架自动推导 enabled 实例技能目录为 bwrap --ro-bind-try 目标。
# tool_shell 插件在 _wrap_sandbox 中消费此 ContextVar。
CURRENT_BWRAP_RO_BIND = SandboxVar[list[str] | None]("nanobee_bwrap_ro_bind", default=None)

# Per-turn 路由上下文
CURRENT_REQUEST_CONTEXT = SandboxVar[RequestContext | None]("nanobee_request_context", default=None)


# ── 模块级别名 —— 保持所有 import 兼容 ────────────────────────────────────────

bind_sandbox = CURRENT_SANDBOX.bind
reset_sandbox = CURRENT_SANDBOX.reset
current_sandbox = CURRENT_SANDBOX.current

bind_tmp = CURRENT_TMP.bind
reset_tmp = CURRENT_TMP.reset
current_tmp = CURRENT_TMP.current

bind_context_root = CURRENT_CONTEXT_ROOT.bind
reset_context_root = CURRENT_CONTEXT_ROOT.reset
current_context_root = CURRENT_CONTEXT_ROOT.current

bind_process_workspace = CURRENT_PROCESS_WORKSPACE.bind
reset_process_workspace = CURRENT_PROCESS_WORKSPACE.reset
current_process_workspace = CURRENT_PROCESS_WORKSPACE.current

bind_bwrap_ro_bind = CURRENT_BWRAP_RO_BIND.bind
reset_bwrap_ro_bind = CURRENT_BWRAP_RO_BIND.reset
current_bwrap_ro_bind = CURRENT_BWRAP_RO_BIND.current

bind_request_context = CURRENT_REQUEST_CONTEXT.bind
reset_request_context = CURRENT_REQUEST_CONTEXT.reset
current_request_context = CURRENT_REQUEST_CONTEXT.current

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
