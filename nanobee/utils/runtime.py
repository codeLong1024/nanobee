"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nanobee.utils.logger import logger


from nanobee.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

# Third same-target workspace violation in a turn escalates to "stop retrying".
_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)

# 截断工具参数的错误 tool result 文案前缀。
# PR-B 在 truncated(length) 轮发现参数被截断时，不执行该工具调用，
# 而合成一条 is_error tool result 告知模型参数不完整、需完整重发。
TRUNCATED_ARGS_ERROR_MESSAGE = (
    "参数被截断未执行 — 此工具调用的参数在生成过程中被输出长度限制截断。"
    "请勿以文本代替工具调用，请使用完整参数重新发起此工具调用。"
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_length_recovery_message(tool_names: list[str] | None = None) -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit.

    Args:
        tool_names: When provided, lists the specific tool call(s) whose
            arguments were truncated. The recovery prompt is parameterized
            to tell the model which tool to re-send with complete arguments.
            When empty, falls back to the generic continuation prompt.
    """
    if tool_names:
        names = ", ".join(tool_names)
        msg = (
            "Output limit reached. The following tool call argument(s) were "
            f"truncated before completion: {names}. "
            "Please re-send these tool call(s) with complete arguments, "
            "and continue exactly where you left off."
        )
        return {"role": "user", "content": msg}
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def _extract_lookup_key(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """从工具参数中提取标准化查找键，工具无关，只关心参数中的 URL/query。"""
    url = str(arguments.get("url") or "").strip()
    if url:
        return url.lower()
    query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
    if query:
        return query.lower()
    return None


def external_lookup_signature(
    tool_name: str,
    arguments: dict[str, Any],
    throttled_tools: dict[str, str],
) -> str | None:
    """Stable signature for repeated external lookups we want to throttle.

    Args:
        tool_name: 工具名称
        arguments: 工具参数
        throttled_tools: 工具名→节流组名的映射，由调用方从插件元数据构建。

    Returns:
        节流签名 ``{group}:{key}``，不匹配时返回 None。
    """
    group = throttled_tools.get(tool_name)
    if group is None:
        return None
    key = _extract_lookup_key(tool_name, arguments)
    if key is None:
        return None
    return f"{group}:{key}"


def repeated_external_lookup_error(
    tool_name: str,
    arguments: dict[str, Any],
    seen_counts: dict[str, int],
    throttled_tools: dict[str, str],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments, throttled_tools)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


# Workspace-boundary violations are soft errors, with per-target throttling.

_OUTSIDE_PATH_PATTERN = re.compile(r"(?:^|[\s|>'\"])((?:/[^\s\"'>;|<]+)|(?:~[^\s\"'>;|<]+))")


def workspace_violation_signature(
    tool_name: str,
    arguments: dict[str, Any],
    exec_capable_tools: set[str],
) -> str | None:
    """Return a stable cross-tool signature for the outside-workspace target.

    Args:
        tool_name: 工具名称
        arguments: 工具参数
        exec_capable_tools: 具有命令执行能力的工具名集合，由调用方从插件元数据构建。
    """
    for key in ("path", "file_path", "target", "source", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_violation_target(val.strip())

    if tool_name in exec_capable_tools:
        cmd = str(arguments.get("command") or "").strip()
        if cmd:
            match = _OUTSIDE_PATH_PATTERN.search(cmd)
            if match:
                return _normalize_violation_target(match.group(1))
        cwd = str(arguments.get("working_dir") or "").strip()
        if cwd:
            return _normalize_violation_target(cwd)

    return None


def _normalize_violation_target(raw: str) -> str:
    """Normalize *raw* path so that equivalent spellings collide on the same key."""
    try:
        normalized = Path(raw).expanduser().resolve().as_posix()
    except Exception:
        normalized = raw.replace("\\", "/")
    return f"violation:{normalized}".lower()


def repeated_workspace_violation_error(
    tool_name: str,
    arguments: dict[str, Any],
    seen_counts: dict[str, int],
    exec_capable_tools: set[str],
) -> str | None:
    """Return an escalated error after repeated bypass attempts."""
    signature = workspace_violation_signature(tool_name, arguments, exec_capable_tools)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_WORKSPACE_VIOLATIONS:
        return None
    logger.warning(
        "Escalating repeated workspace bypass attempt {} (attempt {})",
        signature[:160],
        count,
    )
    target = signature.split("violation:", 1)[1] if "violation:" in signature else signature
    return (
        "Error: refusing repeated workspace-bypass attempts.\n"
        f"You have tried to access '{target}' (or an equivalent path) "
        f"{count} times in this turn. This is a hard policy boundary -- "
        "switching tools, shell tricks, working_dir overrides, symlinks, "
        "or base64 piping will NOT change the answer. Stop retrying. "
        "If the user genuinely needs this resource, tell them you cannot "
        "access it and ask how they want to proceed (e.g. copy the file "
        "into the workspace directory)."
    )
