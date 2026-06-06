"""Nanobee 统一异常层次结构。

所有自定义异常均继承自 ``NanobeeError``，调用方可用 ``except NanobeeError``
统一捕获框架内所有已知异常。

按模块领域组织：
- Kernel 层：PluginError / RouteError / SandboxViolationError / SoulViolationError / ContextError
- Provider 层：ProviderError 及其子类
- Agent 层：AgentError 及其子类
- 存储层：StorageError 及其子类
"""

from __future__ import annotations


class NanobeeError(Exception):
    """所有 Nanobee 异常的基类。"""

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        self.detail = detail or {}
        super().__init__(message)


# ── Kernel 层 ──────────────────────────────────────────────

class PluginError(NanobeeError):
    """插件加载/注册/生命周期错误。"""


class PluginNotFoundError(PluginError):
    """指定插件未找到。"""


class RouteError(NanobeeError):
    """消息路由错误。"""


class UnknownRouteError(RouteError):
    """消息无法路由到任何已注册的处理器。"""

    def __init__(self, channel: str, chat_id: str) -> None:
        self.channel = channel
        self.chat_id = chat_id
        super().__init__(
            f"未知路由: channel={channel!r}, chat_id={chat_id!r}",
            detail={"channel": channel, "chat_id": chat_id},
        )


class SandboxViolationError(NanobeeError):
    """沙箱拦截：文件操作超出允许的路径范围。"""

    def __init__(self, path: str, context_root: str, detail: str = "") -> None:
        self.path = path
        self.context_root = context_root
        message = f"沙箱拦截: 路径 {path!r} 超出用户上下文 {context_root!r}"
        if detail:
            message += f"（{detail}）"
        super().__init__(
            message,
            detail={"path": path, "context_root": context_root, "sub_detail": detail},
        )


class SoulViolationError(NanobeeError):
    """灵魂文件（core.md）被篡改或违反约束。"""


class ContextError(NanobeeError):
    """上下文处理错误（内核状态、生命周期等）。"""


# ── Provider 层 ──────────────────────────────────────────

class ProviderError(NanobeeError):
    """LLM Provider 相关错误。"""


class ProviderConfigError(ProviderError):
    """Provider 配置缺失或无效。"""


class ProviderAuthError(ProviderError):
    """Provider 认证失败（token 过期、device flow 拒绝等）。"""


class ProviderTimeoutError(ProviderError):
    """Provider 请求超时。"""


class ProviderAPIError(ProviderError):
    """Provider API 返回错误。"""

    def __init__(self, message: str, *, status_code: int = 0, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message, detail={"status_code": status_code, "body": body})


class ImageGenerationError(ProviderError):
    """图片生成失败。"""


class CodexHTTPError(ProviderAPIError):
    """OpenAI Codex HTTP 错误（含 retry_after）。"""

    def __init__(self, message: str, *, status_code: int = 0, body: str = "",
                 retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message, status_code=status_code, body=body)


# ── Agent 层 ──────────────────────────────────────────────

class AgentError(NanobeeError):
    """Agent 执行错误。"""


class ToolExecutionError(AgentError):
    """工具插件执行失败。"""


class ToolViolationError(AgentError):
    """工具调用违反安全策略（SSRF/工作区越界）。"""


class LoopStateError(AgentError):
    """Agent 状态机转换错误。"""


# ── 存储层 ────────────────────────────────────────────────

class StorageError(NanobeeError):
    """存储/文件操作错误。"""


class ArtifactError(StorageError):
    """媒体文件存储/解码错误。"""


__all__ = [
    # 基类
    "NanobeeError",
    # Kernel
    "PluginError",
    "PluginNotFoundError",
    "RouteError",
    "UnknownRouteError",
    "SandboxViolationError",
    "SoulViolationError",
    "ContextError",
    # Provider
    "ProviderError",
    "ProviderConfigError",
    "ProviderAuthError",
    "ProviderTimeoutError",
    "ProviderAPIError",
    "ImageGenerationError",
    "CodexHTTPError",
    # Agent
    "AgentError",
    "ToolExecutionError",
    "ToolViolationError",
    "LoopStateError",
    # Storage
    "StorageError",
    "ArtifactError",
]
