"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from nanobee.agent.hook import AgentHookContext


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": getattr(tool_call, "arguments", {}) or {},
        "detail": "",
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }


def build_tool_event_end_payload(
    tool_call: Any, event: dict[str, Any],
) -> dict[str, Any]:
    """构造工具结束进度事件（phase=end/error）。

    与 :func:`build_tool_event_start_payload` 字段对称；工具结果不跨层传递
    （可能达数十 KB），摘要走 ``detail``。``files``/``embeds`` 恒为空列表:
    进度事件只承载「步骤状态」，不承载产物清单。

    Args:
        tool_call: 工具调用请求（提供 id/name/arguments）。
        event: 工具流水线产出的执行事件（含 ``status``/``detail``）。

    Returns:
        进度事件载荷（``phase`` 由 ``event["status"]`` 推导）。
    """
    phase = "end" if event.get("status") == "ok" else "error"
    detail = str(event.get("detail", "") or "")
    return {
        "version": 1,
        "phase": phase,
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": getattr(tool_call, "arguments", {}) or {},
        "detail": detail,
        "result": None,
        "error": None if phase == "end" else detail,
        "files": [],
        "embeds": [],
    }


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    files = result.get("files") if isinstance(result.get("files"), list) else []
    embeds = result.get("embeds") if isinstance(result.get("embeds"), list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx] if isinstance(context.tool_events[idx], dict) else {}
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": getattr(tool_call, "arguments", {}) or {},
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
        }
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        payloads.append(payload)
    return payloads
