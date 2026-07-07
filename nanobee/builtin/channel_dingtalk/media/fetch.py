"""Remote media fetching with SSRF protection and file reading.

Provides:
- ``fetch_remote_media_bytes()`` — download remote media with redirect protection
- ``read_media_bytes()`` — read media from URL or local file
- SSRF validation helpers
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .helpers import guess_filename, guess_upload_type, is_http_url


def validate_url_target(url: str) -> tuple[bool, str]:
    """简易 URL 安全检查：仅允许 HTTP/HTTPS，拒绝私有 IP。"""
    if not url.startswith(("http://", "https://")):
        return False, "not an HTTP URL"
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "local address blocked"
    if host.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                         "172.20.", "172.21.", "172.22.", "172.23.",
                         "172.24.", "172.25.", "172.26.", "172.27.",
                         "172.28.", "172.29.", "172.30.", "172.31.",
                         "192.168.")):
        return False, "private address blocked"
    return True, ""


async def fetch_remote_media_bytes(
    http: httpx.AsyncClient | None,
    media_ref: str,
    logger: Any = None,
    *,
    max_bytes: int = 20 * 1024 * 1024,
    max_redirects: int = 3,
    allow_remote_media_redirects: bool = False,
    remote_media_redirect_allowed_hosts: set[str] | None = None,
) -> tuple[bytes | None, str | None]:
    """Fetch a remote media URL with SSRF, redirect, and size checks.

    Args:
        http: Shared HTTP client.
        media_ref: Remote URL to fetch.
        logger: Optional logger.
        max_bytes: Maximum allowed response size.
        max_redirects: Maximum number of redirects to follow.
        allow_remote_media_redirects: Whether to follow redirects.
        remote_media_redirect_allowed_hosts: Allowed redirect target hosts.

    Returns:
        ``(data, content_type)`` or ``(None, None)`` on failure.
    """
    if not http:
        return None, None

    if not _validate_remote_media_url(media_ref, logger):
        return None, None

    try:
        stream = getattr(http, "stream", None)
        if stream is not None:
            current_url = media_ref
            for redirect_step in range(max_redirects + 1):
                async with stream("GET", current_url, follow_redirects=False) as resp:
                    final_ok, _ = validate_url_target(str(resp.url))
                    if not final_ok:
                        _warn(logger, "remote media redirect blocked ref={} final={}", media_ref, resp.url)
                        return None, None
                    if 300 <= resp.status_code < 400:
                        current_url = _follow_redirect(
                            str(resp.url), resp.headers.get("location"),
                            media_ref, logger, allow_remote_media_redirects,
                            remote_media_redirect_allowed_hosts,
                        )
                        if not current_url:
                            return None, None
                        continue
                    if resp.status_code >= 400:
                        _warn(logger, "media download failed status={} ref={}", resp.status_code, current_url)
                        return None, None
                    try:
                        return await _stream_response(resp, logger, current_url, max_bytes)
                    except _StreamOverflow:
                        return None, None
            _warn(logger, "media download exceeded redirect limit ref={}", media_ref)
            return None, None

        # Fallback: non-streaming HTTP client
        current_url = media_ref
        for redirect_step in range(max_redirects + 1):
            resp = await http.get(current_url, follow_redirects=False)
            resolved_url = str(getattr(resp, "url", current_url))
            final_ok, _ = validate_url_target(resolved_url)
            if not final_ok:
                return None, None
            if 300 <= resp.status_code < 400:
                current_url = _follow_redirect(
                    resolved_url, resp.headers.get("location"),
                    media_ref, logger, allow_remote_media_redirects,
                    remote_media_redirect_allowed_hosts,
                )
                if not current_url:
                    return None, None
                continue
            if resp.status_code >= 400:
                return None, None
            if len(resp.content) > max_bytes:
                return None, None
            return resp.content, (resp.headers.get("content-type") or "")
        return None, None
    except httpx.TransportError:
        _exc(logger, "media download network error ref={}", media_ref)
        raise
    except Exception:
        _exc(logger, "media download error ref={}", media_ref)
        return None, None


async def read_media_bytes(
    http: httpx.AsyncClient | None,
    media_ref: str,
    logger: Any = None,
    **kwargs: Any,
) -> tuple[bytes | None, str | None, str | None]:
    """Read media bytes from URL or local file.

    Args:
        http: Shared HTTP client.
        media_ref: URL or local path.
        logger: Optional logger.

    Returns:
        ``(data, filename, content_type)`` or ``(None, None, None)``.
    """
    if not media_ref:
        return None, None, None

    if is_http_url(media_ref):
        data, raw_content_type = await fetch_remote_media_bytes(http, media_ref, logger, **kwargs)
        if data is None:
            return None, None, None
        content_type = (raw_content_type or "").split(";")[0].strip()
        filename = guess_filename(media_ref, guess_upload_type(media_ref))
        return data, filename, content_type or None

    # Handle local files
    try:
        if media_ref.startswith("file://"):
            parsed = urlparse(media_ref)
            local_path = Path(unquote(parsed.path))
        else:
            local_path = Path(os.path.expanduser(media_ref))
        if not local_path.is_file():
            _warn(logger, "media file not found: {}", local_path)
            return None, None, None
        data = await asyncio.to_thread(local_path.read_bytes)
        content_type = mimetypes.guess_type(local_path.name)[0]
        return data, local_path.name, content_type
    except Exception:
        _exc(logger, "media read error ref={}", media_ref)
        return None, None, None


# ==================== SSRF helpers ====================


def _validate_remote_media_url(media_ref: str, logger: Any = None) -> bool:
    """Validate remote media URL for SSRF protection."""
    ok, err = validate_url_target(media_ref)
    if not ok:
        _warn(logger, "remote media URL blocked ref={} reason={}", media_ref, err)
        return False
    return True


def _redirect_host_allowed(
    current_url: str,
    next_url: str,
    allowed_hosts: set[str] | None = None,
) -> bool:
    """Check if redirect host is allowed."""
    current_host = (urlparse(current_url).hostname or "").lower()
    next_host = (urlparse(next_url).hostname or "").lower()
    if not next_host:
        return False
    if next_host == current_host:
        return True
    if allowed_hosts:
        return next_host in allowed_hosts
    return False


def _next_remote_media_url(
    current_url: str,
    location: str | None,
    *,
    logger: Any = None,
    allow_redirects: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str | None:
    """Calculate next URL for media download redirect."""
    if not allow_redirects:
        _warn(logger, "media download redirect refused ref={}", current_url)
        return None
    if not location:
        _warn(logger, "media download redirect without Location ref={}", current_url)
        return None
    next_url = urljoin(current_url, location)
    if not _redirect_host_allowed(current_url, next_url, allowed_hosts):
        _warn(logger, "media download cross-host redirect refused ref={} next={}", current_url, next_url)
        return None
    if not _validate_remote_media_url(next_url, logger):
        return None
    return next_url


# ==================== Redirect helper ====================


def _follow_redirect(
    current_url: str,
    location: str | None,
    original_ref: str,
    logger: Any = None,
    allow_redirects: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str | None:
    """解析重定向，返回下一跳 URL 或 None（表示中止下载）。"""
    return _next_remote_media_url(
        current_url, location,
        logger=logger,
        allow_redirects=allow_redirects,
        allowed_hosts=allowed_hosts,
    )


async def _stream_response(
    resp: httpx.Response,
    logger: Any,
    current_url: str,
    max_bytes: int,
) -> tuple[bytes, str]:
    """流式读取响应体并做大小检查，返回 (bytes, content_type)。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            _warn(logger, "media download too large ref={} bytes>{}", current_url, max_bytes)
            raise _StreamOverflow()
        chunks.append(chunk)
    return b"".join(chunks), (resp.headers.get("content-type") or "")


class _StreamOverflow(Exception):
    """流式读取超过 max_bytes 的内部信号。"""


# ==================== Logger helpers ====================


def _warn(logger: Any, msg: str, *args: Any) -> None:
    if logger:
        logger.warning(msg, *args)


def _exc(logger: Any, msg: str, *args: Any) -> None:
    if logger:
        logger.exception(msg, *args)


__all__ = [
    "fetch_remote_media_bytes",
    "read_media_bytes",
]
