"""Remote media fetching with SSRF protection and file reading.

Provides:
- ``fetch_remote_media_bytes()`` — download remote media with redirect protection
- ``read_media_bytes()`` — read media from URL or local file

安全策略在本模块单点生效——所有媒体读取路径（`_send_media_ref`、marker 管线、
raw path、本地图片替换）都经 :func:`read_media_bytes`：

- ``allow_media=False`` 时本地与远端一律拒绝（``enable_media_upload`` 总开关）。
- 远端 URL 委托 :mod:`nanobee.security.network` 的解析级校验（DNS 解析后逐 IP
  判定私网/回环/链路本地/云元数据/保留段，覆盖十进制、八进制、十六进制 IP 与
  IPv6 映射地址），重定向逐跳复检；DNS 属阻塞调用，统一放入线程执行，不占用事件循环。
- 本地文件必须落在显式注入的白名单根内（``data_dir`` + ``media_local_roots`` +
  入站附件目录），且读取量受体积上限约束（分块读取，累计超限即中止；返回的完整
  bytes 供上传层一次使用，故内存峰值以该上限为界）。

策略值由 :class:`~nanobee.builtin.channel_dingtalk.sender.DingTalkSender` 在通道
启动时按 ``DingTalkConfig`` 注入（``enable_media_upload`` / ``media_max_mb`` /
``media_local_roots``）。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import stat as stat_mod
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .helpers import guess_filename, guess_upload_type, is_http_url

from nanobee.security.network import validate_resolved_url, validate_url_target
from nanobee.security.workspace_policy import is_path_allowed

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
"""媒体体积上限缺省值（20MB）。调用方按 ``media_max_mb`` 注入实际值。"""

_READ_CHUNK_BYTES = 1024 * 1024
"""本地分块读取块大小（1MB）——超限即中止，不读取上限之外的内容。"""


async def fetch_remote_media_bytes(
    http: httpx.AsyncClient | None,
    media_ref: str,
    logger: Any = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
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

    if not await _validate_remote_media_url(media_ref, logger):
        return None, None

    try:
        stream = getattr(http, "stream", None)
        if stream is not None:
            current_url = media_ref
            for redirect_step in range(max_redirects + 1):
                async with stream("GET", current_url, follow_redirects=False) as resp:
                    final_ok, final_err = await _validate_resolved_url(str(resp.url))
                    if not final_ok:
                        _warn(logger, "remote media redirect blocked ref={} final={} reason={}",
                              media_ref, resp.url, final_err)
                        return None, None
                    if 300 <= resp.status_code < 400:
                        current_url = await _follow_redirect(
                            str(resp.url), resp.headers.get("location"),
                            logger, allow_remote_media_redirects,
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
            final_ok, final_err = await _validate_resolved_url(resolved_url)
            if not final_ok:
                _warn(logger, "remote media redirect blocked ref={} final={} reason={}",
                      media_ref, resolved_url, final_err)
                return None, None
            if 300 <= resp.status_code < 400:
                current_url = await _follow_redirect(
                    resolved_url, resp.headers.get("location"),
                    logger, allow_remote_media_redirects,
                    remote_media_redirect_allowed_hosts,
                )
                if not current_url:
                    return None, None
                continue
            if resp.status_code >= 400:
                _warn(logger, "media download failed status={} ref={}", resp.status_code, current_url)
                return None, None
            if len(resp.content) > max_bytes:
                _warn(logger, "media download too large ref={} bytes>{}", current_url, max_bytes)
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
    *,
    max_bytes: int | None = None,
    local_roots: Sequence[str | Path] | None = None,
    allow_media: bool = True,
    **kwargs: Any,
) -> tuple[bytes | None, str | None, str | None]:
    """Read media bytes from URL or local file.

    Args:
        http: Shared HTTP client.
        media_ref: URL or local path.
        logger: Optional logger.
        max_bytes: 体积上限（字节）。缺省用 :data:`DEFAULT_MAX_BYTES`。
        local_roots: 本地文件白名单根目录（已解析的绝对路径）。缺省拒绝一切本地读取。
        allow_media: ``False`` 时本地与远端一律拒绝（``enable_media_upload`` 总开关）。
        **kwargs: 透传给 :func:`fetch_remote_media_bytes` 的远端选项。

    Returns:
        ``(data, filename, content_type)`` or ``(None, None, None)``.
    """
    if not media_ref:
        return None, None, None

    if not allow_media:
        _warn(logger, "media upload disabled, refused ref={}", media_ref)
        return None, None, None

    limit = max_bytes if max_bytes and max_bytes > 0 else DEFAULT_MAX_BYTES

    if is_http_url(media_ref):
        data, raw_content_type = await fetch_remote_media_bytes(
            http, media_ref, logger, max_bytes=limit, **kwargs,
        )
        if data is None:
            return None, None, None
        content_type = (raw_content_type or "").split(";")[0].strip()
        filename = guess_filename(media_ref, guess_upload_type(media_ref))
        return data, filename, content_type or None

    # Handle local files（白名单 + 体积上限 + 分块读取；解析与判定走线程，避免阻塞事件循环）
    local_path, reject_reason = await asyncio.to_thread(
        _resolve_allowed_local_path, media_ref, local_roots,
    )
    if local_path is None:
        if reject_reason == "no_roots":
            _warn(logger, "local media roots not configured, refused ref={}", media_ref)
        elif reject_reason == "outside_roots":
            _warn(logger, "local media path outside allowed roots, refused ref={}", media_ref)
        else:
            _warn(logger, "invalid local media path ref={}", media_ref)
        return None, None, None

    data = await _read_local_limited(local_path, limit, logger)
    if data is None:
        return None, None, None
    content_type = mimetypes.guess_type(local_path.name)[0]
    return data, local_path.name, content_type


# ==================== Local file helpers ====================


def _resolve_local_path(media_ref: str) -> Path | None:
    """把媒体引用解析为绝对路径（``file://`` 与普通路径同等对待）。

    ``resolve(strict=False)`` 会跟随符号链接并归一化 ``..``——越界判定与
    后续打开使用同一路径，避免"检查一个路径、读另一个路径"。
    """
    try:
        if media_ref.startswith("file://"):
            raw = unquote(urlparse(media_ref).path)
        else:
            raw = media_ref
        return Path(os.path.expanduser(raw)).resolve(strict=False)
    except (OSError, ValueError):
        return None


def _resolve_allowed_local_path(
    media_ref: str,
    roots: Sequence[str | Path] | None,
) -> tuple[Path | None, str]:
    """解析本地路径并做白名单判定（同步，供 ``asyncio.to_thread`` 调用）。

    未注入任何根（``None`` / 空）时一律拒绝：宁可显式失败，也不做"默认全放行"
    的静默兜底（默认放行会让白名单失效而不自知）。

    Returns:
        ``(path, "")`` 通过；否则 ``(None, 拒绝原因)``。
    """
    path = _resolve_local_path(media_ref)
    if path is None:
        return None, "invalid"
    if not roots:
        return None, "no_roots"
    if not is_path_allowed(path, roots):
        return None, "outside_roots"
    return path, ""


async def _read_local_limited(path: Path, limit: int, logger: Any) -> bytes | None:
    """带体积上限的本地读取：``stat`` 预检 + 分块读取兜底。"""
    size = await asyncio.to_thread(_regular_file_size, path)
    if size is None:
        _warn(logger, "media file not found: {}", path)
        return None
    if size > limit:
        _warn(logger, "media file too large ref={} bytes={} limit={}", path, size, limit)
        return None
    try:
        data = await asyncio.to_thread(_read_limited_sync, path, limit)
    except OSError:
        _exc(logger, "media read error ref={}", path)
        return None
    if data is None:
        # 预检与读取之间文件被替换/增长：仍拒绝，不返回被截断的内容
        _warn(logger, "media file exceeded limit while reading ref={}", path)
    return data


def _regular_file_size(path: Path) -> int | None:
    """返回常规文件大小（字节）；不存在或非常规文件返回 ``None``。"""
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat_mod.S_ISREG(st.st_mode):
        return None
    return st.st_size


def _read_limited_sync(path: Path, limit: int) -> bytes | None:
    """分块读取本地文件，累计超过 ``limit`` 立即中止并返回 ``None``。

    返回值为完整 bytes（上传层需要一次性数据），因此内存峰值以 ``limit`` 为界；
    分块的意义在于"不读取上限之外的内容"，而不是避免最终物化。

    ``O_NOFOLLOW`` 拒绝最终组件为符号链接（路径已在 :func:`_resolve_local_path`
    解析过，此处仅关闭"解析后再换成链接"的竞态窗口；不支持该标志的平台自动降级）。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        buffer = bytearray()
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            if len(buffer) + len(chunk) > limit:
                return None
            buffer.extend(chunk)
        return bytes(buffer)
    finally:
        os.close(fd)


# ==================== SSRF helpers ====================


async def _validate_remote_media_url(media_ref: str, logger: Any = None) -> bool:
    """Validate remote media URL for SSRF protection.

    委托 :func:`nanobee.security.network.validate_url_target`——框架内唯一的
    解析级 SSRF 判定（含 CIDR 白名单 ``tools.ssrfWhitelist``），本模块不再
    自持第二套字符串黑名单。DNS 解析是阻塞调用，放入线程执行。
    """
    ok, err = await asyncio.to_thread(validate_url_target, media_ref)
    if not ok:
        _warn(logger, "remote media URL blocked ref={} reason={}", media_ref, err)
        return False
    return True


async def _validate_resolved_url(url: str) -> tuple[bool, str]:
    """重定向后的地址复检（同样把 DNS 解析移出事件循环）。"""
    return await asyncio.to_thread(validate_resolved_url, url)


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


async def _next_remote_media_url(
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
    if not await _validate_remote_media_url(next_url, logger):
        return None
    return next_url


# ==================== Redirect helper ====================


async def _follow_redirect(
    current_url: str,
    location: str | None,
    logger: Any = None,
    allow_redirects: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str | None:
    """解析重定向，返回下一跳 URL 或 None（表示中止下载）。"""
    return await _next_remote_media_url(
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
    "DEFAULT_MAX_BYTES",
    "fetch_remote_media_bytes",
    "read_media_bytes",
]
