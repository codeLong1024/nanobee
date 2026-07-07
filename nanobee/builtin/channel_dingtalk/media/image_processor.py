"""Image path processing — ported from services/media/image.ts + media.ts.

Handles Markdown image references to local files and bare image paths:

- ``![alt](/path/to/image.jpg)`` → upload and replace with ``media_id``
- Bare path like ``/path/to/image.jpg`` → wrap in Markdown and upload
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from . import constants as C
from .helpers import get_logger, async_re_sub


async def _upload_and_replace_image(
    text: str,
    regex: re.Pattern,
    sender,
    token: str,
    build_replacement: Callable[[re.Match, str, str, str], str],
    error_label: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """通用图片上传替换流水线：匹配 → 读文件 → 上传 → 替换。

    Args:
        text: 输入文本。
        regex: 匹配图片引用的正则。
        sender: ``DingTalkSender`` 实例。
        token: DingTalk access token。
        build_replacement: ``(match, path, media_id, filename) → replacement_str`` 的回调。
        error_label: 日志中区分类型的前缀（如 ``"local image"`` / ``"bare image"``）。
        logger: 可选的 logger。
    """
    _log = logger or get_logger(__name__)

    async def _replacer(match: re.Match) -> str:
        path = match.group(match.lastindex or 1)

        # Skip remote / data URLs
        if path.startswith(("http://", "https://", "data:")):
            return match.group(0)

        data, filename, content_type = await sender.read_media_bytes(path)
        if not data:
            _log.warning("{}: could not read {}", error_label, path)
            return match.group(0)

        media_id = await sender.upload_media(
            token=token, data=data,
            media_type="image", filename=filename or "image.jpg",
            content_type=content_type,
        )
        if not media_id:
            _log.warning("{}: upload failed for {}", error_label, path)
            return match.group(0)

        return build_replacement(match, path, media_id, filename or "image.jpg")

    return await async_re_sub(regex, _replacer, text)


async def process_local_images(
    text: str,
    sender,
    token: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Process Markdown local image references in AI response text.

    Matches ``![alt](local_path)`` and replaces with ``![alt](media_id)``.
    """

    def _repl(match: re.Match, path: str, media_id: str) -> str:
        alt = match.group(1) or match.group(2)
        return f"![{alt}]({media_id})"

    return await _upload_and_replace_image(
        text, C.LOCAL_IMAGE_RE, sender, token, _repl,
        error_label="local image", logger=logger,
    )


async def process_bare_image_paths(
    text: str,
    sender,
    token: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Process bare local image paths not wrapped in Markdown syntax.

    Uploads and wraps in ``![filename](media_id)``.
    """

    def _repl(match: re.Match, path: str, media_id: str, filename: str) -> str:
        return f"![{filename}]({media_id})"

    return await _upload_and_replace_image(
        text, C.BARE_IMAGE_PATH_RE, sender, token, _repl,
        error_label="bare image", logger=logger,
    )


__all__ = [
    "process_local_images",
    "process_bare_image_paths",
]
