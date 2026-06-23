"""Pure helper functions for DingTalk media handling.

All functions are stateless — no dependency on ``DingTalkSender``.
Extension sets are exported alongside helpers for reuse across modules.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ============ Extension sets (shared across sender / constants / helpers) ============

IMAGE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
AUDIO_EXTS: set[str] = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}
VIDEO_EXTS: set[str] = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
ZIP_BEFORE_UPLOAD_EXTS: set[str] = {".htm", ".html"}

# ============ URL helpers ============


def is_http_url(value: str) -> bool:
    """Check if a value is an HTTP URL."""
    return urlparse(value).scheme in ("http", "https")


def guess_upload_type(media_ref: str) -> str:
    """Guess DingTalk upload type from file extension."""
    ext = Path(urlparse(media_ref).path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "voice"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def guess_filename(media_ref: str, upload_type: str) -> str:
    """Guess filename from media reference."""
    name = os.path.basename(urlparse(media_ref).path)
    return (
        name
        or {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}.get(upload_type, "file.bin")
    )


# ============ File helpers ============


def zip_bytes(filename: str, data: bytes) -> tuple[bytes, str, str]:
    """Zip a file before upload (for HTML files)."""
    stem = Path(filename).stem or "attachment"
    safe_name = filename or "attachment.bin"
    zip_name = f"{stem}.zip"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(safe_name, data)
    return buffer.getvalue(), zip_name, "application/zip"


def normalize_upload_payload(
    filename: str,
    data: bytes,
    content_type: str | None,
    logger: Any = None,
) -> tuple[bytes, str, str | None]:
    """Normalize upload payload, zipping HTML files if needed."""
    ext = Path(filename).suffix.lower()
    if ext in ZIP_BEFORE_UPLOAD_EXTS or content_type == "text/html":
        if logger:
            logger.info(
                "'does not accept raw HTML attachments, zipping {} before upload'",
                filename,
            )
        data, filename, content_type = zip_bytes(filename, data)
    return data, filename, content_type


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a logger instance for the media subpackage.

    Args:
        name: Logger name. Defaults to ``__name__`` of caller's module.

    Returns:
        A :class:`logging.Logger` instance.
    """
    return logging.getLogger(name)


async def async_re_sub(
    pattern: re.Pattern,
    async_replacer: Any,
    text: str,
) -> str:
    """Apply an async replacer function over all regex matches.

    Args:
        pattern: Compiled regular expression pattern.
        async_replacer: Async callable that receives a :class:`re.Match`
            and returns the replacement string.
        text: Input text to search and replace in.

    Returns:
        The transformed string with all matches replaced.
    """
    result_parts: list[str] = []
    last_end = 0
    for match in pattern.finditer(text):
        result_parts.append(text[last_end:match.start()])
        replacement = await async_replacer(match)
        result_parts.append(replacement)
        last_end = match.end()
    result_parts.append(text[last_end:])
    return "".join(result_parts)


__all__ = [
    "IMAGE_EXTS",
    "AUDIO_EXTS",
    "VIDEO_EXTS",
    "ZIP_BEFORE_UPLOAD_EXTS",
    "is_http_url",
    "guess_upload_type",
    "guess_filename",
    "zip_bytes",
    "normalize_upload_payload",
    "get_logger",
    "async_re_sub",
]
