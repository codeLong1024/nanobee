"""Shared lifecycle hook primitives for agent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import logging

logger = logging.getLogger(__name__)

from nanobee.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    streamed_content: bool = False
    streamed_reasoning: bool = False
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None


class AgentHook:
    """Minimal lifecycle surface for shared runner customization."""

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        pass

    async def emit_reasoning_end(self) -> None:
        """Mark the end of an in-flight reasoning stream.

        Hooks that buffer ``emit_reasoning`` chunks (for in-place UI updates)
        flush and freeze the rendered group here. One-shot hooks ignore.
        """
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content


class CompositeHook(AgentHook):
    """Fan-out hook that delegates to an ordered list of hooks.

    Error isolation: async methods catch and log per-hook exceptions
    so a faulty custom hook cannot crash the agent loop.
    ``finalize_content`` is a pipeline (no isolation — bugs should surface).
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                await getattr(h, method_name)(*args, **kwargs)
                continue

            try:
                await getattr(h, method_name)(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.%s error in %s", method_name, type(h).__name__)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._for_each_hook_safe("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._for_each_hook_safe("emit_reasoning_end")

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content


class SDKCaptureHook(AgentHook):
    """Record tool names and the final message list for ``RunResult``.

    The runner mutates ``context.messages`` in place across iterations, so the
    snapshot is refreshed on every ``after_iteration`` call; the last call
    reflects the end-of-turn state the SDK caller cares about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools_used: list[str] = []
        self.messages: list[dict[str, Any]] = []

    async def after_iteration(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            self.tools_used.append(call.name)
        self.messages = list(context.messages)


class StreamBridgeHook(AgentHook):
    """桥接 Kernel.handle_message 的流式回调 → AgentHook 系统。

    仅桥接 on_stream（流式增量），不桥接 on_stream_end。
    结束事件由调用者返回后统一处理，
    避免运行器内部的 on_stream_end 与通道的二次发送产生时序冲突。
    """

    def __init__(self, on_stream: Any = None, on_stream_end: Any = None) -> None:
        super().__init__()
        self._on_stream = on_stream
        # on_stream_end 不使用：运行器内部触发会导致重复 _stream_end
        # 改为由 handle_message 返回后调用者统一发送
        _ = on_stream_end

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_stream(self, context: Any, delta: str) -> None:
        if self._on_stream and delta:
            try:
                await self._on_stream(delta)
            except Exception:
                logger.exception("[StreamBridgeHook] on_stream callback failed, delta=%s...", delta[:80])

    async def on_stream_end(self, context: Any, *, resuming: bool = False) -> None:
        # no-op：结束事件由 handle_message 返回后统一处理
        pass

    def finalize_content(self, context: Any, content: str | None) -> str | None:
        """Pass-through: StreamBridgeHook does not modify content."""
        return content
