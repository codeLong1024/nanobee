"""Media upload — normal + chunked upload, ported from services/media/common.ts + chunk-upload.ts.

Supports two upload paths:
1. **Normal upload** (≤20 MB): delegates to :meth:`DingTalkSender.upload_media`
2. **Chunked upload** (>20 MB): three-step DingTalk protocol:
   a. enableUploadTransaction  → get upload_transaction_id
   b. uploadFileBlock           → upload chunks sequentially
   c. submitUploadTransaction   → merge chunks into media_id
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import httpx

from .helpers import get_logger

# Chunked upload configuration (ported from chunk-upload.ts)
CHUNK_MIN = 100 * 1024  # 100 KB
CHUNK_MAX = 8 * 1024 * 1024  # 8 MB
CHUNK_DEFAULT = 5 * 1024 * 1024  # 5 MB
CHUNK_THRESHOLD = 20 * 1024 * 1024  # 20 MB — files above this use chunked upload


async def upload_media(
    http: httpx.AsyncClient,
    token: str,
    data: bytes,
    media_type: str,
    filename: str,
    content_type: str | None = None,
    file_size: int | None = None,
    logger: logging.Logger | None = None,
    agent_id: str = "",
) -> str | None:
    """Upload media to DingTalk, auto-switching to chunk upload for large files.

    Args:
        http: Shared HTTP client (typically ``sender._http``).
        token: DingTalk access token.
        data: Raw file bytes.
        media_type: DingTalk media type (``"image"``/``"voice"``/``"video"``/``"file"``).
        filename: Original filename.
        file_size: File size in bytes (auto-computed from ``data`` if ``None``).
        logger: Optional logger instance.
        agent_id: DingTalk agent ID for chunked upload (robotCode/ClientId).

    Returns:
        Uploaded ``media_id`` string, or ``None`` on failure.
    """
    if file_size is None:
        file_size = len(data)

    if file_size > CHUNK_THRESHOLD:
        _log = logger or get_logger(__name__)
        _log.info(
            "File size {} > threshold {}, using chunked upload for {}",
            file_size, CHUNK_THRESHOLD, filename,
        )
        return await chunk_upload(http, token, data, filename, file_size, agent_id=agent_id, logger=_log)

    #     Normal upload via old OAPI endpoint
    url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
    mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    files = {"media": (filename, data, mime)}

    try:
        resp = await http.post(url, files=files)
        text = resp.text
        result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400:
            _log = logger or get_logger(__name__)
            _log.error(
                "media upload failed status={} type={} body={}",
                resp.status_code, media_type, text[:500],
            )
            return None
        errcode = result.get("errcode", 0)
        if errcode != 0:
            _log = logger or get_logger(__name__)
            _log.error(
                "media upload api error type={} errcode={} body={}",
                media_type, errcode, text[:500],
            )
            return None
        sub = result.get("result") or {}
        media_id = result.get("media_id") or result.get("mediaId") or sub.get("media_id") or sub.get("mediaId")
        if not media_id:
            _log = logger or get_logger(__name__)
            _log.error("media upload missing media_id body={}", text[:500])
            return None
        return str(media_id)
    except httpx.TransportError:
        _log = logger or get_logger(__name__)
        _log.exception("media upload network error type={}", media_type)
        raise


# ---------------------------------------------------------------------------
# Chunked upload — three-step DingTalk protocol
# ---------------------------------------------------------------------------


async def chunk_upload(
    http: httpx.AsyncClient,
    token: str,
    data: bytes,
    filename: str,
    file_size: int,
    agent_id: str = "",
    logger: logging.Logger | None = None,
) -> str | None:
    """Chunked upload for files > 20 MB.

    Implements the three-step DingTalk protocol:
    1. ``enableUploadTransaction`` — obtain a transaction ID
    2. ``uploadFileBlock`` — upload each chunk
    3. ``submitUploadTransaction`` — merge chunks

    Args:
        http: Shared HTTP client.
        token: DingTalk access token.
        data: Raw file bytes.
        filename: Original filename.
        file_size: Total file size in bytes.
        agent_id: DingTalk agent ID for chunked upload.
        logger: Optional logger.

    Returns:
        ``media_id`` string, or ``None`` on failure.
    """
    _log = logger or get_logger(__name__)

    # Step 1: Enable upload transaction
    transaction_id = await enable_upload_transaction(http, token, data, filename, file_size, agent_id, _log)
    if not transaction_id:
        return None

    # Step 2: Upload chunks
    total_chunks = (file_size + CHUNK_DEFAULT - 1) // CHUNK_DEFAULT
    for chunk_index in range(total_chunks):
        offset = chunk_index * CHUNK_DEFAULT
        chunk_data = data[offset:offset + CHUNK_DEFAULT]
        ok = await upload_file_block(http, token, transaction_id, chunk_data, chunk_index, total_chunks, _log)
        if not ok:
            _log.error(
                "chunk upload failed at chunk {}/{} for {}",
                chunk_index, total_chunks, filename,
            )
            return None
        _log.debug("chunk {}/{} uploaded for {}", chunk_index + 1, total_chunks, filename)

    # Step 3: Submit transaction
    return await submit_upload_transaction(http, token, transaction_id, _log)


async def enable_upload_transaction(
    http: httpx.AsyncClient,
    token: str,
    data: bytes,
    filename: str,
    file_size: int,
    agent_id: str = "",
    logger: logging.Logger = None,  # type: ignore[assignment]
) -> str | None:
    """Step 1: Enable upload transaction — obtain an ``uploadTransactionId``.

    POST ``https://api.dingtalk.com/v1.0/file/upload/transaction/enable``

    Body: ``{ agentId, fileSize, fileName, fileMd5, addDentry }``

    Args:
        http: Shared HTTP client.
        token: DingTalk access token.
        data: Raw file bytes (used to compute MD5).
        filename: Original filename.
        file_size: Total file size.
        agent_id: DingTalk agent ID (robotCode/ClientId). Required by API.
        logger: Logger instance.

    Returns:
        ``uploadTransactionId`` string, or ``None`` on failure.
    """
    _log = logger or get_logger(__name__)
    if not agent_id:
        _log.warning("agentId is empty — DingTalk chunked upload API may reject this request")
    md5 = hashlib.md5(data).hexdigest()
    payload = {
        "agentId": agent_id,
        "fileSize": file_size,
        "fileName": filename,
        "fileMd5": md5,
        "addDentry": False,
    }
    headers = {"x-acs-dingtalk-access-token": token}
    try:
        resp = await http.post(
            "https://api.dingtalk.com/v1.0/file/upload/transaction/enable",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                "enableUploadTransaction failed: status={} body={}",
                resp.status_code, resp.text[:500],
            )
            return None
        result = resp.json()
        transaction_id = result.get("uploadTransactionId")
        if not transaction_id:
            logger.error("enableUploadTransaction missing uploadTransactionId: {}", resp.text[:500])
            return None
        logger.info("enableUploadTransaction success: transaction_id={}", transaction_id)
        return str(transaction_id)
    except httpx.TransportError:
        logger.exception("enableUploadTransaction network error")
        raise


async def upload_file_block(
    http: httpx.AsyncClient,
    token: str,
    transaction_id: str,
    chunk_data: bytes,
    chunk_index: int,
    total_chunks: int,
    logger: logging.Logger,
) -> bool:
    """Step 2: Upload a single file block.

    POST ``https://api.dingtalk.com/v1.0/file/upload/chunk``

    Uses multipart/form-data with fields:
    - ``uploadTransactionId``
    - ``chunkIndex``
    - ``totalChunks``
    - ``file`` (binary)

    Args:
        http: Shared HTTP client.
        token: DingTalk access token.
        transaction_id: Transaction ID from step 1.
        chunk_data: Raw bytes for this chunk.
        chunk_index: Zero-based chunk index.
        total_chunks: Total number of chunks.
        logger: Logger instance.

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    headers = {"x-acs-dingtalk-access-token": token}
    payload = {
        "uploadTransactionId": transaction_id,
        "chunkIndex": str(chunk_index),
        "totalChunks": str(total_chunks),
    }
    files = {"file": ("chunk", chunk_data, "application/octet-stream")}
    try:
        resp = await http.post(
            "https://api.dingtalk.com/v1.0/file/upload/chunk",
            data=payload,
            files=files,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                "uploadFileBlock failed: status={} chunk={}/{} body={}",
                resp.status_code, chunk_index, total_chunks, resp.text[:500],
            )
            return False
        return True
    except httpx.TransportError:
        logger.exception("uploadFileBlock network error chunk={}/{}", chunk_index, total_chunks)
        raise


async def submit_upload_transaction(
    http: httpx.AsyncClient,
    token: str,
    transaction_id: str,
    logger: logging.Logger,
) -> str | None:
    """Step 3: Submit upload transaction — merge chunks into a media_id.

    GET ``https://api.dingtalk.com/v1.0/file/upload/transaction/submit?uploadTransactionId={id}``

    Args:
        http: Shared HTTP client.
        token: DingTalk access token.
        transaction_id: Transaction ID from step 1.
        logger: Logger instance.

    Returns:
        ``mediaId`` string, or ``None`` on failure.
    """
    headers = {"x-acs-dingtalk-access-token": token}
    try:
        resp = await http.get(
            "https://api.dingtalk.com/v1.0/file/upload/transaction/submit",
            params={"uploadTransactionId": transaction_id},
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                "submitUploadTransaction failed: status={} body={}",
                resp.status_code, resp.text[:500],
            )
            return None
        result = resp.json()
        if result.get("success"):
            media_id = result.get("mediaId")
            if media_id:
                logger.info("submitUploadTransaction success: media_id={}", media_id)
                return str(media_id)
            logger.error("submitUploadTransaction missing mediaId: {}", resp.text[:500])
            return None
        logger.error("submitUploadTransaction not successful: {}", resp.text[:500])
        return None
    except httpx.TransportError:
        logger.exception("submitUploadTransaction network error")
        raise


__all__ = [
    "upload_media",
    "chunk_upload",
    "enable_upload_transaction",
    "upload_file_block",
    "submit_upload_transaction",
    "CHUNK_DEFAULT",
    "CHUNK_THRESHOLD",
]
