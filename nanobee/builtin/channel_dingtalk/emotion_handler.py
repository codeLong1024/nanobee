"""Thinking emoji management for DingTalk messages.

Adds a 🤔 thinking emoji to the message while processing, and recalls
it when done — providing visual feedback that the bot is working.

Uses DingTalk Robot Emotion API:
- POST /v1.0/robot/emotion/reply    (add)
- POST /v1.0/robot/emotion/recall   (recall)

Retry strategy (aligned with openclaw-channel-dingtalk):
- attach: 3 attempts [0ms, 400ms, 1200ms]
- recall: 3 attempts [0ms, 1500ms, 5000ms]
- retryable: HTTP 5xx or errorCode "system.err"
- update_emotion: recall old → attach new → fallback restore default on failure

Reference: docs/dingtalk/emotion_reply.py
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from nanobee.utils.logger import logger


# Retry delays for emotion API operations (seconds).
# Aligned with openclaw: attach [0, 400, 1200]ms, recall [0, 1500, 5000]ms.
_ATTACH_RETRY_DELAYS: tuple[float, ...] = (0.0, 0.4, 1.2)
_RECALL_RETRY_DELAYS: tuple[float, ...] = (0.0, 1.5, 5.0)

# Default emotion payload — used as the "thinking" reaction baseline.
_EMOTION_PAYLOAD: dict[str, Any] = {
    "emotionType": 2,
    "emotionName": "🤔思考中",
    "textEmotion": {
        "emotionId": "2659900",
        "emotionName": "🤔思考中",
        "text": "🤔思考中",
        "backgroundId": "im_bg_1",
    },
}

# Token source type: either a pre-resolved string or an async callable
# that returns a fresh token on each invocation.
_TokenSrc = str | Callable[[], Awaitable[str | None]]


def _build_body(
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
    emotion_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the emotion API request body (matching reference)."""
    payload = emotion_payload if emotion_payload is not None else _EMOTION_PAYLOAD
    return {
        "robotCode": robot_code,
        "openMsgId": open_msg_id,
        "openConversationId": open_conversation_id,
        **payload,
    }


def _is_retryable_emotion_error(status_code: int, response_text: str) -> bool:
    """判断钉钉 emotion API 错误是否可重试。

    可重试条件：
    - HTTP 5xx 服务器错误（瞬时故障，重试通常可恢复）
    - 响应体 errorCode 为 ``"system.err"``（钉钉内部临时错误）

    不可重试示例：4xx 客户端错误（参数错误等，重试无意义）。
    """
    if status_code >= 500:
        return True
    try:
        body = json.loads(response_text)
        error_code = str(body.get("code", "")).strip().lower()
        if error_code == "system.err":
            return True
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return False


async def _resolve_token(token_src: _TokenSrc) -> str | None:
    """将 token 源解析为实际 token 字符串。

    支持两种形式：
    - 字符串：直接返回（预取 token）
    - 异步 callable：每次调用重新获取（支持 token 刷新）
    """
    if callable(token_src):
        return await token_src()
    return token_src


async def _emotion_api_request(
    http_client: Any,
    token: _TokenSrc,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
    action: str = "add",
    payload_override: dict[str, Any] | None = None,
    retry_delays: tuple[float, ...] = (),
) -> bool:
    """Send an emotion API request to DingTalk with retry support.

    Args:
        action: ``"add"`` → POST /robot/emotion/reply,
                ``"recall"`` → POST /robot/emotion/recall.
        payload_override: Optional custom emotion payload to use instead of
                the default ``_EMOTION_PAYLOAD``.
        token: Either a pre-resolved access token string, or an async
                callable that returns a fresh token per attempt.
        retry_delays: Retry delay sequence in seconds.  Length determines
                max attempts (e.g. ``(0, 0.4, 1.2)`` → 3 attempts).

    Returns:
        True on success, False on failure after all retries exhausted.
    """
    if not open_msg_id or not open_conversation_id:
        logger.warning(
            "[Emotion] Skipped ({}): missing open_msg_id={} or open_conversation_id={}",
            action, open_msg_id, open_conversation_id,
        )
        return False

    endpoint = "reply" if action == "add" else "recall"
    url = f"https://api.dingtalk.com/v1.0/robot/emotion/{endpoint}"

    emotion_payload = payload_override if payload_override is not None else _EMOTION_PAYLOAD
    body = _build_body(robot_code, open_msg_id, open_conversation_id, emotion_payload=emotion_payload)

    max_attempts = len(retry_delays) if retry_delays else 1
    for attempt in range(max_attempts):
        # 每次重试前等待指定延迟（首次为 0，立即执行）
        delay = retry_delays[attempt] if attempt < len(retry_delays) else 0
        if delay > 0:
            await asyncio.sleep(delay)

        # 每次尝试重新获取 token（支持 token 刷新）
        resolved_token = await _resolve_token(token)
        if not resolved_token:
            logger.warning(
                "[Emotion] {} attempt {}/{}: no token available",
                action, attempt + 1, max_attempts,
            )
            continue

        headers = {
            "x-acs-dingtalk-access-token": resolved_token,
            "Content-Type": "application/json",
        }

        try:
            resp = await http_client.post(url, json=body, headers=headers, timeout=5)
            if resp.status_code == 200:
                logger.debug("[Emotion] {} success: msgId={}", action, open_msg_id)
                return True

            # 非 200：检查是否可重试
            is_last = attempt >= max_attempts - 1
            if not is_last and _is_retryable_emotion_error(resp.status_code, resp.text):
                logger.debug(
                    "[Emotion] {} retryable error (attempt={}/{}): status={}, body={:.200}",
                    action, attempt + 1, max_attempts, resp.status_code, resp.text,
                )
                continue

            logger.warning(
                "[Emotion] {} failed: status={}, body={:.200}",
                action, resp.status_code, resp.text,
            )
            return False

        except httpx.TimeoutException:
            is_last = attempt >= max_attempts - 1
            if not is_last:
                logger.debug(
                    "[Emotion] {} timeout (attempt={}/{}), retrying",
                    action, attempt + 1, max_attempts,
                )
                continue
            logger.warning(
                "[Emotion] {} timeout after {} attempts: msgId={}",
                action, max_attempts, open_msg_id,
            )
            return False

        except httpx.NetworkError:
            is_last = attempt >= max_attempts - 1
            if not is_last:
                logger.debug(
                    "[Emotion] {} network error (attempt={}/{}), retrying",
                    action, attempt + 1, max_attempts,
                )
                continue
            logger.warning(
                "[Emotion] {} network error after {} attempts: msgId={}",
                action, max_attempts, open_msg_id,
            )
            return False

    return False


async def add_thinking_emoji(
    http_client: Any,
    token: _TokenSrc,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
) -> bool:
    """Add a 🤔 thinking emoji to the message via HTTP API.

    Retries up to 3 times with delays [0ms, 400ms, 1200ms].
    Returns True on success.
    """
    return await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="add",
        retry_delays=_ATTACH_RETRY_DELAYS,
    )


async def recall_thinking_emoji(
    http_client: Any,
    token: _TokenSrc,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
) -> bool:
    """Recall the thinking emoji from the message via HTTP API.

    Retries up to 3 times with delays [0ms, 1500ms, 5000ms].
    Returns True on success.
    """
    return await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="recall",
        retry_delays=_RECALL_RETRY_DELAYS,
    )


async def update_emotion(
    http_client: Any,
    token: _TokenSrc,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
    emotion_name: str,
) -> bool:
    """Update the message emotion to a new state (e.g. ✍️输出中, ✅已完成).

    Three-phase strategy:
    1. Recall old emotion (best-effort, with retry)
    2. Attach new emotion (with retry)
    3. If attach fails: restore the default thinking emoji as fallback,
       preventing the message from losing its reaction entirely.

    Returns True if the new emotion was applied successfully.
    """
    logger.debug(
        "[Emotion] Updating to '{}' for msg_id={}",
        emotion_name, open_msg_id,
    )

    # Phase 1: Recall old emotion (best-effort, non-fatal)
    await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="recall",
        retry_delays=_RECALL_RETRY_DELAYS,
    )

    # Phase 2: Build and attach new emotion payload
    payload = dict(_EMOTION_PAYLOAD)
    payload["emotionName"] = emotion_name
    payload["textEmotion"] = {
        "emotionId": _EMOTION_PAYLOAD["textEmotion"]["emotionId"],
        "emotionName": emotion_name,
        "text": emotion_name,
        "backgroundId": _EMOTION_PAYLOAD["textEmotion"]["backgroundId"],
    }

    ok = await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="add",
        payload_override=payload,
        retry_delays=_ATTACH_RETRY_DELAYS,
    )

    if ok:
        return True

    # Phase 3: Fallback — restore default thinking emoji
    # Without this, the old emoji was already recalled but the new one
    # failed to attach → message has no reaction, which is irrecoverable.
    logger.warning(
        "[Emotion] Failed to update to '{}', restoring default thinking emoji "
        "(msg_id={})", emotion_name, open_msg_id,
    )
    restored = await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="add",
        retry_delays=_ATTACH_RETRY_DELAYS,
    )
    if restored:
        logger.debug(
            "[Emotion] Fallback restore succeeded (msg_id={})", open_msg_id,
        )
    else:
        logger.warning(
            "[Emotion] Fallback restore also failed (msg_id={})", open_msg_id,
        )
    return False


__all__ = [
    "add_thinking_emoji",
    "recall_thinking_emoji",
    "update_emotion",
]
