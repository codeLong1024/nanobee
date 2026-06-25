"""Inbound media download.

Provides:
- ``download_image_with_inferred_ext()`` — download with content-type extension inference
- ``download_dingtalk_file()`` — download file from DingTalk download code
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

_DINGTALK_MEDIA_DIR: Path | None = None


def _get_media_dir(subdir: str) -> Path:
    """获取媒体文件下载目录。"""
    global _DINGTALK_MEDIA_DIR
    if _DINGTALK_MEDIA_DIR is None:
        _DINGTALK_MEDIA_DIR = Path(".") / "media" / subdir
    return _DINGTALK_MEDIA_DIR


async def download_dingtalk_file(
    http: httpx.AsyncClient | None,
    token: str | None,
    client_id: str,
    download_code: str,
    filename: str,
    sender_id: str,
    logger: Any = None,
    *,
    retries: int = 2,
    connect_timeout: float = 15.0,
    read_timeout: float = 120.0,
) -> str | None:
    """Download a DingTalk file to the media directory, return local path.

    Retries on transient network errors up to *retries* times.

    Args:
        http: Shared HTTP client.
        token: DingTalk access token.
        client_id: DingTalk robot Client ID.
        download_code: DingTalk file download code.
        filename: Target filename.
        sender_id: Sender identifier (used for subdirectory naming).
        logger: Optional logger instance.
        retries: Number of retries on transient errors.
        connect_timeout: Connect timeout in seconds.
        read_timeout: Read timeout in seconds.

    Returns:
        Local file path, or ``None`` on failure.
    """
    last_error: Exception | None = None
    for attempt in range(1 + retries):
        url_host = "?"
        try:
            if not token or not http:
                if logger:
                    logger.error("file download: no token or http client")
                return None

            if attempt > 0:
                wait = min(2 ** attempt, 8)
                if logger:
                    logger.info(
                        "'retry {} in {}s: download_code={}'", attempt, wait, download_code[:16],
                    )
                await asyncio.sleep(wait)

            # Step 1: Exchange downloadCode for a temporary download URL
            api_url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
            headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
            payload = {"downloadCode": download_code, "robotCode": client_id}
            resp = await http.post(api_url, json=payload, headers=headers)
            if resp.status_code != 200:
                if logger:
                    logger.error(
                        "'get download URL failed: status={}, body={}'",
                        resp.status_code, resp.text,
                    )
                return None

            result = resp.json()
            download_url = result.get("downloadUrl")
            if not download_url:
                if logger:
                    logger.error("'download URL not found in response: {}'", result)
                return None

            if logger:
                logger.debug("'download URL (full): {}'", download_url)
            url_host = download_url.split("/")[2] if "://" in download_url else "?"

            # Step 2: Download the file content
            timeout = httpx.Timeout(connect_timeout, connect=connect_timeout, read=read_timeout, pool=connect_timeout)
            file_resp = await http.get(
                download_url, follow_redirects=True, timeout=timeout,
            )
            if file_resp.status_code != 200:
                if logger:
                    logger.error("'file download failed: status={}'", file_resp.status_code)
                return None

            # Save to media directory
            download_dir = _get_media_dir("dingtalk") / sender_id
            download_dir.mkdir(parents=True, exist_ok=True)
            file_path = download_dir / filename
            await asyncio.to_thread(file_path.write_bytes, file_resp.content)
            if logger:
                logger.info("'file saved: {}'", file_path)
            return str(file_path)

        except httpx.ConnectTimeout as e:
            last_error = e
            if logger:
                logger.warning(
                    "[{}/{}] ConnectTimeout connecting to file server "
                    "'(may be blocked by proxy/firewall): host={}'",
                    attempt + 1, 1 + retries, url_host,
                )
        except httpx.ReadTimeout as e:
            last_error = e
            if logger:
                logger.warning(
                    "'[{}/{}] ReadTimeout downloading file (may be too large): {}'",
                    attempt + 1, 1 + retries, e,
                )
        except httpx.ConnectError as e:
            last_error = e
            if logger:
                logger.warning(
                    "'[{}/{}] ConnectError — check network/proxy settings: host={}'",
                    attempt + 1, 1 + retries, url_host,
                )
        except Exception:
            last_error = None  # Non-retryable; log immediately and bail
            if logger:
                logger.exception("file download error")
            return None

    # All retries exhausted
    if logger:
        logger.error(
            "'file download failed after {}/{} attempts: host={}'",
            1 + retries, 1 + retries, url_host,
        )
    return None


__all__ = [
    "download_dingtalk_file",
]
