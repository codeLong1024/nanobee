"""Raw media path detection — ported from services/media.ts:processRawMediaPaths().

Detects bare file paths in AI response text that were **not** wrapped in
markers or Markdown syntax, then uploads them and sends as native DingTalk
media messages.

This is a **safety net**: if the AI outputs a raw file path like
``/tmp/generated_report.pdf`` without any marker syntax, this module catches
it and ensures the file is sent to the user.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import constants as C
from .helpers import get_logger


async def process_raw_media_paths(
    text: str,
    sender,
    token: str,
    chat_id: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Detect and process bare media paths left in AI response text.

    Flow (matching TS ``processRawMediaPaths``):

    1. Scan text for bare video/audio/file paths using regex patterns.
    2. Verify the path points to an existing local file.
    3. Upload and send each one as a native DingTalk media message.
    4. Remove the path from the text.

    Args:
        text: The AI response text (possibly containing bare paths).
        sender: ``DingTalkSender`` instance.
        token: DingTalk access token.
        chat_id: Target chat ID.
        logger: Optional logger.

    Returns:
        Text with bare paths removed (paths replaced by native media sends).
    """
    _log = logger or get_logger(__name__)

    # Collect all bare paths with their media types and positions
    path_entries: list[tuple[str, str, int, int]] = []

    for match in C.RAW_VIDEO_PATH_RE.finditer(text):
        raw = match.group(1) if match.lastindex else match.group(0)
        path_entries.append((raw, "video", match.start(), match.end()))
    for match in C.RAW_AUDIO_PATH_RE.finditer(text):
        raw = match.group(1) if match.lastindex else match.group(0)
        path_entries.append((raw, "voice", match.start(), match.end()))
    for match in C.RAW_FILE_PATH_RE.finditer(text):
        raw = match.group(1) if match.lastindex else match.group(0)
        path_entries.append((raw, "file", match.start(), match.end()))

    # 从后往前替换，避免位置偏移
    path_entries.sort(key=lambda x: x[2], reverse=True)
    for raw_path, media_type, start, end in path_entries:
        p = Path(raw_path)
        if not p.exists():
            _log.debug("raw path: file does not exist, skipping {}", raw_path)
            continue

        data, filename, content_type = await sender.read_media_bytes(raw_path)
        if not data:
            _log.warning("raw path: could not read {}", raw_path)
            continue

        media_id = await sender.upload_media(
            token=token, data=data,
            media_type=media_type,
            filename=filename or p.name,
            content_type=content_type,
        )
        if not media_id:
            _log.warning("raw path: upload failed for {}", raw_path)
            continue

        # Send native media message
        if media_type in ("video", "voice"):
            success = await _send_native_media(
                sender, token, chat_id, media_id, media_type, p.name,
            )
        else:
            success = await sender._send_batch_message(
                token, chat_id, "sampleFile",
                {"mediaId": media_id, "fileName": filename or p.name, "fileType": media_type},
            )

        if success:
            text = text[:start] + text[end:]
            _log.info("raw path: sent {} as {}", raw_path, media_type)
        else:
            _log.warning("raw path: send failed for {}", raw_path)

    return text


async def _send_native_media(
    sender,
    token: str,
    chat_id: str,
    media_id: str,
    media_type: str,
    filename: str,
) -> bool:
    """Send a native video or voice message via batch API.

    Args:
        sender: ``DingTalkSender`` instance.
        token: DingTalk access token.
        chat_id: Target chat.
        media_id: Uploaded media ID.
        media_type: ``"video"`` or ``"voice"``.
        filename: Original filename (for logging).

    Returns:
        ``True`` on success.
    """
    if media_type == "voice":
        msg_param = {"mediaId": media_id, "duration": 0}
        return await sender._send_batch_message(token, chat_id, "sampleVoice", msg_param)
    else:
        msg_param = {"mediaId": media_id, "thumbMediaId": "", "duration": 0}
        return await sender._send_batch_message(token, chat_id, "sampleVideo", msg_param)


__all__ = ["process_raw_media_paths"]
