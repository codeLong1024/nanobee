"""Interactive card management for DingTalk — full AI Card flow.

- Two-step card creation: create + deliver via /card/instances + /card/instances/deliver
- Streaming via /card/streaming (typing effect)
- Status updates via /card/instances (INPUTING / FINISHED / FAILED)
- Uses DingTalkCardClient for token + HTTP management
"""

from __future__ import annotations

from nanobee.utils.logger import logger


import json
import random
import string
import time
from typing import Any

import httpx

from .card_client import DingTalkCardClient
from .models import AICardStatus

# DingTalk AI Card template ID (official template)
AI_CARD_TEMPLATE_ID = "02fcf2f4-5e02-4a85-b672-46d1f715543e.schema"


class CardManager:
    """DingTalk AI Card manager.

    Features:
    - Two-step card creation (create + deliver)
    - Streaming content via /card/streaming (typing effect)
    - Status management (INPUTING / FINISHED / FAILED)
    - Card instance tracking
    """

    def __init__(self, card_client: DingTalkCardClient) -> None:
        self.client = card_client

    @staticmethod
    def generate_track_id() -> str:
        """Generate a unique track ID for a card instance."""
        return f"card_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"

    # ------------------------------------------------------------------
    # Card lifecycle
    # ------------------------------------------------------------------

    async def create_card(
        self,
        card_instance_id: str,
        robot_code: str,
        target: dict[str, str],
    ) -> str:
        """Create and deliver an AI Card to the target conversation.

        Two-step flow:
        1. POST /card/instances (create card instance)
        2. POST /card/instances/deliver (deliver to conversation)

        Returns the card_instance_id on success.
        Raises on failure.
        """
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        # Step 1: Create card instance
        create_body: dict[str, Any] = {
            "cardTemplateId": AI_CARD_TEMPLATE_ID,
            "outTrackId": card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "config": json.dumps({"autoLayout": True}, ensure_ascii=False),
                }
            },
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": True},
            "imRobotOpenSpaceModel": {"supportForward": True},
        }

        logger.debug("'[CARD] Creating AI Card: {}'", card_instance_id)
        resp = await client.post(
            f"{self.client.api_url}/card/instances",
            headers=headers,
            json=create_body,
        )
        logger.info("'[CARD] Create response: status={} body={}'", resp.status_code, resp.text[:500])
        await self.client.check_response(resp, f"[{card_instance_id}] Create card")
        if resp.status_code != 200:
            raise RuntimeError(f"Card creation failed: {resp.status_code} - {resp.text[:500]}")

        # Step 2: Deliver to target conversation
        deliver_body = self._build_deliver_body(card_instance_id, target, robot_code)
        logger.debug("'[CARD] Deliver body: {}'", json.dumps(deliver_body, ensure_ascii=False))
        resp = await client.post(
            f"{self.client.api_url}/card/instances/deliver",
            headers=headers,
            json=deliver_body,
        )
        logger.info("'[CARD] Deliver response: status={} body={}'", resp.status_code, resp.text[:500])
        await self.client.check_response(resp, f"[{card_instance_id}] Deliver card")
        if resp.status_code != 200:
            raise RuntimeError(f"Card delivery failed: {resp.status_code} - {resp.text[:200]}")

        resp_data = resp.json()
        if not resp_data.get("success"):
            error_msg = resp_data.get("result", [{}])[0].get("errorMsg", "unknown")
            raise RuntimeError(f"Card delivery rejected: {error_msg}")

        logger.info("'[CARD] Created + delivered card {}'", card_instance_id)
        return card_instance_id

    def _build_deliver_body(
        self,
        card_instance_id: str,
        target: dict[str, str],
        robot_code: str,
    ) -> dict[str, Any]:
        """Build delivery body."""
        receiver_user_id = target.get("receiverUserId", "")

        if receiver_user_id:
            open_space_id = f"dtv1.card//IM_ROBOT.{receiver_user_id}"
            return {
                "outTrackId": card_instance_id,
                "userIdType": 1,
                "openSpaceId": open_space_id,
                "imRobotOpenDeliverModel": {
                    "spaceType": "IM_ROBOT",
                    "robotCode": robot_code,
                    "extension": {"dynamicSummary": "true"},
                },
            }
        else:
            open_space_id = f"dtv1.card//IM_GROUP.{target['openConversationId']}"
            return {
                "outTrackId": card_instance_id,
                "userIdType": 1,
                "openSpaceId": open_space_id,
                "imGroupOpenDeliverModel": {
                    "robotCode": robot_code,
                },
            }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def start_streaming(self, card_instance_id: str, content: str = "") -> None:
        """Switch card to INPUTING streaming status.

        PUT /card/instances with flowStatus=INPUTING.
        """
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        status_body: dict[str, Any] = {
            "outTrackId": card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "flowStatus": AICardStatus.INPUTING,
                    "msgContent": content,
                    "staticMsgContent": "",
                    "sys_full_json_obj": json.dumps(
                        {"order": ["msgContent"]}, ensure_ascii=False,
                    ),
                    "config": json.dumps({"autoLayout": True}, ensure_ascii=False),
                }
            },
        }

        logger.debug("'[CARD] start_streaming {}'", card_instance_id)
        resp = await client.put(
            f"{self.client.api_url}/card/instances",
            headers=headers,
            json=status_body,
        )
        await self.client.check_response(resp, f"[{card_instance_id}] Start streaming")
        resp.raise_for_status()

    async def stream_content(
        self, card_instance_id: str, content: str, is_final: bool = False,
    ) -> None:
        """Push incremental content via /card/streaming (typing effect).

        Sends the FULL accumulated content each time — DingTalk renders
        it progressively.
        """
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        body: dict[str, Any] = {
            "outTrackId": card_instance_id,
            "guid": f"{int(time.time() * 1000)}_{self._random_str(6)}",
            "key": "msgContent",
            "content": content,
            "isFull": True,
            "isFinalize": is_final,
            "isError": False,
        }

        try:
            resp = await client.put(
                f"{self.client.api_url}/card/streaming",
                headers=headers,
                json=body,
            )
            await self.client.check_response(resp, f"[{card_instance_id}] Stream content")
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and "QpsLimit" in e.response.text:
                # Retry once on QPS limit
                resp = await client.put(
                    f"{self.client.api_url}/card/streaming",
                    headers=headers,
                    json=body,
                )
                await self.client.check_response(resp, f"[{card_instance_id}] Stream retry")
                resp.raise_for_status()
            else:
                raise

    async def finish_streaming(
        self, card_instance_id: str, final_content: str,
    ) -> None:
        """Finalize card with FINISHED status via PUT /card/instances."""
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        finish_body: dict[str, Any] = {
            "outTrackId": card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "flowStatus": AICardStatus.FINISHED,
                    "msgContent": final_content,
                    "staticMsgContent": "",
                    "sys_full_json_obj": json.dumps(
                        {"order": ["msgContent"]}, ensure_ascii=False,
                    ),
                    "config": json.dumps({"autoLayout": True}, ensure_ascii=False),
                }
            },
            "cardUpdateOptions": {"updateCardDataByKey": True},
        }

        logger.debug("'[CARD] finish_streaming {} ({} chars)'", card_instance_id, len(final_content))
        resp = await client.put(
            f"{self.client.api_url}/card/instances",
            headers=headers,
            json=finish_body,
        )
        await self.client.check_response(resp, f"[{card_instance_id}] Finish streaming")
        resp.raise_for_status()

    async def fail_card(self, card_instance_id: str, error_message: str) -> None:
        """Mark card as FAILED."""
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        fail_body: dict[str, Any] = {
            "outTrackId": card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "flowStatus": AICardStatus.FAILED,
                    "msgContent": f"处理失败: {error_message}",
                }
            },
        }

        logger.warning("'[CARD] fail_card {}: {}'", card_instance_id, error_message)
        try:
            resp = await client.put(
                f"{self.client.api_url}/card/instances",
                headers=headers,
                json=fail_body,
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("'[CARD] fail_card error {}'", card_instance_id)

    # ------------------------------------------------------------------
    # Fallback: non-streaming card update (backward compat)
    # ------------------------------------------------------------------

    async def finalize_card(self, card_instance_id: str, final_content: str) -> bool:
        """Non-streaming finalize — 通过 /card/streaming 推送内容并关闭卡片。

        分两步：
        1. stream_content() → PUT /card/streaming，将内容推入卡片渲染管线
        2. finish_streaming() → PUT /card/instances，设置 FINISHED 状态

        不能跳过 stream_content 直接调 finish_streaming：finish_streaming 用的
        是 /card/instances 端点，虽然携带 msgContent，但钉钉卡片 UI 只渲染通过
        /card/streaming 推送的内容。
        """
        try:
            await self.stream_content(card_instance_id, final_content)
            await self.finish_streaming(card_instance_id, final_content)
            return True
        except Exception:
            logger.exception("'[CARD] finalize_card failed {}'", card_instance_id)
            return False

    async def finish_card_status(self, card_instance_id: str) -> None:
        """仅将卡片状态设为 FINISHED，不修改已有内容。

        用于卡片已通过流式推送积累内容后，兜底消息不应该覆盖卡片内容
        （如 max_iterations 触发的系统通知）。
        """
        client = await self.client.ensure_async_client()
        headers = await self.client.get_headers_async()

        body: dict[str, Any] = {
            "outTrackId": card_instance_id,
            "cardData": {
                "cardParamMap": {
                    "flowStatus": AICardStatus.FINISHED,
                }
            },
            "cardUpdateOptions": {"updateCardDataByKey": True},
        }

        logger.debug("'[CARD] finish_card_status {}'", card_instance_id)
        resp = await client.put(
            f"{self.client.api_url}/card/instances",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _random_str(length: int = 8) -> str:
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))


__all__ = ["CardManager"]
