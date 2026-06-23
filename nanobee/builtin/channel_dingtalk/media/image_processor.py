"""Image path processing — ported from services/media/image.ts + media.ts.

Handles Markdown image references to local files and bare image paths:

- ``![alt](/path/to/image.jpg)`` → upload and replace with ``media_id``
- Bare path like ``/path/to/image.jpg`` → wrap in Markdown and upload
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from . import constants as C
from .helpers import get_logger, async_re_sub


async def process_local_images(
    text: str,
    sender,
    token: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Process Markdown local image references in AI response text.

    Matches ``![alt](local_path)`` and:

    1. Skips HTTP/HTTPS/data URLs (pass through unchanged).
    2. Reads the local file and uploads it to DingTalk.
    3. Replaces the Markdown reference with ``![alt](media_id)``.

    Note:
        Only handles :term:`Markdown <Markdown>` image syntax.
        Bare image paths are handled by :func:`process_bare_image_paths`.
    """
    _log = logger or get_logger(__name__)

    async def _replace_image(match: re.Match) -> str:
        alt = match.group(1)
        path = match.group(2)

        # Skip remote / data URLs
        if path.startswith(("http://", "https://", "data:")):
            return match.group(0)

        data, filename, content_type = await sender.read_media_bytes(path)
        if not data:
            _log.warning("local image: could not read {}", path)
            return match.group(0)

        media_id = await sender.upload_media(
            token=token, data=data,
            media_type="image", filename=filename or "image.jpg",
            content_type=content_type,
        )
        if not media_id:
            _log.warning("local image: upload failed for {}", path)
            return match.group(0)

        return f"![{alt}]({media_id})"

    return await async_re_sub(C.LOCAL_IMAGE_RE, _replace_image, text)


async def process_bare_image_paths(
    text: str,
    sender,
    token: str,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Process bare local image paths not wrapped in Markdown syntax.

    Looks for paths like ``/data/images/screenshot.png`` and:

    1. Uploads the image to DingTalk.
    2. Wraps it in Markdown image syntax ``![filename](media_id)``.

    Only processes absolute paths matching image extensions. HTTP/Base64
    references are passed through unchanged.
    """
    _log = logger or get_logger(__name__)

    async def _replace_bare(match: re.Match) -> str:
        path = match.group(1)
        if path.startswith(("http://", "https://", "data:")):
            return match.group(0)

        data, filename, content_type = await sender.read_media_bytes(path)
        if not data:
            _log.warning("bare image: could not read {}", path)
            return match.group(0)

        media_id = await sender.upload_media(
            token=token, data=data,
            media_type="image", filename=filename or "image.jpg",
            content_type=content_type,
        )
        if media_id:
            return f"![{filename}]({media_id})"
        _log.warning("bare image: upload failed for {}", path)
        return match.group(0)

    return await async_re_sub(C.BARE_IMAGE_PATH_RE, _replace_bare, text)


__all__ = [
    "process_local_images",
    "process_bare_image_paths",
]
