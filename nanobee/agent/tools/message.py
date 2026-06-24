"""Message tool — send messages with optional file attachments.

Allows the LLM to send structured messages with file attachments.
File paths are ultimately picked up by ``_assemble_outbound()``
and delivered through the channel's send mechanism.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from nanobee.agent.tools.base import Tool, tool_parameters
from nanobee.utils.logger import logger



@tool_parameters({
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Message text content to send to the user",
        },
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "List of local file paths (absolute) or URLs to attach. "
                "Example: [\"/path/to/report.pdf\", \"/path/to/image.png\"]"
            ),
        },
    },
    "required": ["content"],
})
class MessageTool(Tool):
    """Send a message to the user, with optional file attachments.

    This tool acts as a structured marker: the actual sending is handled
    by ``AgentLoop._assemble_outbound()``, which scans all conversation
    messages for ``message`` tool calls and collects the ``media`` paths
    into the final ``OutboundMessage``.
    """

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the user, with optional file attachments.\n\n"
            "Use this when you need to:\n"
            "- Send a file/attachment to the user (e.g. generated report, image, document)\n"
            "- Deliver content and files together in one message\n\n"
            "Put the file's absolute path in the media list, and the system will\n"
            "automatically upload and send it as a native file message."
        )

    async def execute(self, **kwargs: Any) -> str:
        """Execute the message tool.

        The tool returns a summary of what was queued; the actual delivery
        is deferred to ``_assemble_outbound()``.
        """
        content = kwargs.get("content", "")
        media = kwargs.get("media", [])

        # 校验 media 路径：本地文件必须存在，避免 LLM 以为发送成功但实际传了无效路径
        invalid_paths: list[str] = []
        valid_media: list[str] = []
        for p in media or []:
            if not isinstance(p, str):
                invalid_paths.append(repr(p))
                continue
            # HTTP/HTTPS URL 不校验存在性（需要在发送阶段验证）
            if p.startswith(("http://", "https://")):
                valid_media.append(p)
                continue
            path = Path(p)
            if path.is_absolute() and not path.exists():
                invalid_paths.append(p)
                continue
            valid_media.append(p)

        if invalid_paths:
            return (
                f"错误：以下 media 文件路径不存在或无效：{', '.join(invalid_paths)}。"
                f"请检查文件是否已正确生成，并传入有效的绝对路径。"
            )

        # Build a friendly summary for the LLM
        filename_hints: list[str] = []
        for path in valid_media:
            try:
                filename_hints.append(Path(path).name)
            except Exception:
                filename_hints.append(path)

        content_preview = (content[:80] + "...") if len(content) > 80 else content

        if filename_hints:
            file_list = ", ".join(filename_hints)
            return (
                f"已将消息加入发送队列：'{content_preview}'，附件：{file_list}"
            )
        return f"已将消息加入发送队列：'{content_preview}'"


def collect_message_tool_media(
    all_msgs: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Extract content and media from ``message`` tool calls in conversation history.

    Scans ``all_msgs`` for assistant messages that contain ``message``
    tool calls, and collects the ``content`` and ``media`` arguments.

    Returns:
        ``(tool_content, media_paths)`` where ``tool_content`` is the
        ``content`` from the **last** ``message`` tool call (if any),
        and ``media_paths`` is a deduplicated list of all media paths.
    """
    tool_content: str | None = None
    media_paths: list[str] = []
    seen_media: set[str] = set()

    for msg in reversed(all_msgs):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if func.get("name") != "message":
                continue
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue

            # Capture content (last call wins for content)
            if args.get("content"):
                tool_content = args["content"]

            # Collect media (all calls contribute)
            for p in args.get("media", []):
                if isinstance(p, str) and p not in seen_media:
                    media_paths.append(p)
                    seen_media.add(p)

    # Reverse back to original order
    media_paths.reverse()
    return tool_content, media_paths


__all__ = ["MessageTool", "collect_message_tool_media"]
