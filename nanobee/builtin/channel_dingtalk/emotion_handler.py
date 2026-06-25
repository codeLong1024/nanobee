"""Thinking emoji management for DingTalk messages.

Adds a 🤔 thinking emoji to the message while processing, and recalls
it when done — providing visual feedback that the bot is working.

Uses DingTalk Robot Emotion API:
- POST /v1.0/robot/emotion/reply    (add)
- POST /v1.0/robot/emotion/recall   (recall)

Reference: docs/dingtalk/emotion_reply.py
"""

from __future__ import annotations

from typing import Any

import httpx

from nanobee.utils.logger import logger


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


async def _emotion_api_request(
    http_client: Any,
    token: str,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
    action: str = "add",
    payload_override: dict[str, Any] | None = None,
) -> None:
    """Send an emotion API request to DingTalk.

    Args:
        action: ``"add"`` → POST /robot/emotion/reply,
                ``"recall"`` → POST /robot/emotion/recall.
        payload_override: Optional custom emotion payload to use instead of
                the default ``_EMOTION_PAYLOAD``.
    """
    if not open_msg_id or not open_conversation_id:
        logger.warning(
            "[Emotion] Skipped ({}): missing open_msg_id={} or open_conversation_id={}",
            action, open_msg_id, open_conversation_id,
        )
        return

    endpoint = "reply" if action == "add" else "recall"
    url = f"https://api.dingtalk.com/v1.0/robot/emotion/{endpoint}"

    emotion_payload = payload_override if payload_override is not None else _EMOTION_PAYLOAD
    body = _build_body(robot_code, open_msg_id, open_conversation_id, emotion_payload=emotion_payload)
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }

    try:
        resp = await http_client.post(url, json=body, headers=headers, timeout=5)
        if resp.status_code == 200:
            logger.debug("[Emotion] {} success: msgId={}", action, open_msg_id)
        else:
            logger.warning(
                "[Emotion] {} failed (non-fatal): status={}, body={:.200}",
                action, resp.status_code, resp.text,
            )
    except httpx.TimeoutException:
        logger.warning("[Emotion] {} timeout (non-fatal): msgId={}", action, open_msg_id)
    except httpx.NetworkError:
        logger.warning("[Emotion] {} network error (non-fatal): msgId={}", action, open_msg_id)
    except Exception as e:
        logger.warning("[Emotion] {} error (non-fatal): {}", action, e)


async def add_thinking_emoji(
    http_client: Any,
    token: str,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
) -> None:
    """Add a 🤔 thinking emoji to the message via HTTP API."""
    await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="add",
    )


async def recall_thinking_emoji(
    http_client: Any,
    token: str,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
) -> None:
    """Recall the thinking emoji from the message via HTTP API."""
    await _emotion_api_request(
        http_client, token, robot_code,
        open_msg_id, open_conversation_id,
        action="recall",
    )


async def update_emotion(
    http_client: Any,
    token: str,
    robot_code: str,
    open_msg_id: str,
    open_conversation_id: str,
    emotion_name: str,
) -> None:
    """Update the message emotion to a new state (e.g. ✍️输出中, ✅已完成).

    Uses a two-phase (recall + add) strategy to simulate an update since the
    DingTalk API does not support direct emotion replacement.
    """
    logger.debug(
        "[Emotion] Updating to '{}' for msg_id={}",
        emotion_name, open_msg_id,
    )

    # Step 1: Recall old emotion (best-effort — failure is non-fatal)
    try:
        await _emotion_api_request(
            http_client, token, robot_code,
            open_msg_id, open_conversation_id,
            action="recall",
        )
    except Exception:
        logger.debug("[Emotion] recall skipped (will proceed with add)")

    # Step 2: Add new emotion (with one retry on failure)
    payload = dict(_EMOTION_PAYLOAD)
    payload["emotionName"] = emotion_name
    payload["textEmotion"] = {
        "emotionId": _EMOTION_PAYLOAD["textEmotion"]["emotionId"],
        "emotionName": emotion_name,
        "text": emotion_name,
        "backgroundId": _EMOTION_PAYLOAD["textEmotion"]["backgroundId"],
    }
    for attempt in range(2):
        try:
            await _emotion_api_request(
                http_client, token, robot_code,
                open_msg_id, open_conversation_id,
                action="add",
                payload_override=payload,
            )
            break
        except Exception:
            if attempt == 0:
                logger.warning("[Emotion] add '{}' failed, retrying…", emotion_name)
            else:
                logger.exception("[Emotion] add '{}' failed after retry", emotion_name)
                raise


__all__ = [
    "add_thinking_emoji",
    "recall_thinking_emoji",
    "update_emotion",
]
