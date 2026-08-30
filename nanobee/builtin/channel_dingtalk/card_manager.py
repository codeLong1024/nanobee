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

        Args:
            card_instance_id: 卡片实例 ID。
            content: 推送的完整内容。
            is_final: 是否为最后一次流式更新（isFinalize）。
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

        logger.debug(
            "'[CARD-DEBUG] stream_content card={} isFinalize={} content={!r}'",
            card_instance_id, is_final, content[:200],
        )
        try:
            resp = await client.put(
                f"{self.client.api_url}/card/streaming",
                headers=headers,
                json=body,
            )
            logger.debug(
                "'[CARD-DEBUG] stream_content resp card={} status={} body={}'",
                card_instance_id, resp.status_code, (resp.text or "")[:500],
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

        logger.debug(
            "'[CARD] finish_streaming {} ({} chars) body={}'",
            card_instance_id, len(final_content), json.dumps(finish_body, ensure_ascii=False)[:500],
        )
        resp = await client.put(
            f"{self.client.api_url}/card/instances",
            headers=headers,
            json=finish_body,
        )
        logger.debug(
            "'[CARD-DEBUG] finish_streaming resp card={} status={} body={}'",
            card_instance_id, resp.status_code, (resp.text or "")[:500],
        )
        await self.client.check_response(resp, f"[{card_instance_id}] Finish streaming")
        resp.raise_for_status()

    async def fail_card(self, card_instance_id: str, error_message: str) -> bool:
        """以 FINISHED 态完结失败卡片，展示错误文案。

        依据钉钉官方 SDK（dingtalk_stream.card_instance.AIMarkdownCardInstance.ai_fail）
        的语义：``flowStatus=FAILED`` 的卡片官方仅携带 ``msgTitle``/``logo``，
        不携带 ``msgContent``，客户端对 FAILED 态的处理是终止并清空内容，导致
        卡片"一闪而过"。因此要让错误文案可见，卡片必须用 ``flowStatus=FINISHED``
        （与正常完成一致），仅将 msgContent 替换为错误文案。

        两步式：
        1. stream_content 推错误文案（isFinalize=True，停止打字机）
        2. finish_streaming 置 flowStatus=FINISHED + 错误文案

        Args:
            card_instance_id: 卡片实例 ID。
            error_message: 要展示的错误文案（调用方已拼好半截进度 + 失败提示）。

        Returns:
            两步是否全部成功。False 表示卡片未能终态化（调用方应回落
            markdown 文本兜底，避免卡片永久停在 INPUTING 且用户零感知）。
        """
        fail_content = f"处理失败: {error_message}"

        logger.warning(
            "'[CARD] fail_card {}: msg={} final_content={!r}'",
            card_instance_id, error_message, fail_content,
        )
        try:
            # 第一步：推错误文案到渲染管线（isFinalize=True 停止打字机）
            await self.stream_content(card_instance_id, fail_content, is_final=True)
            # 第二步：置 flowStatus=FINISHED（复用正常完成路径，仅内容为错误文案）
            await self.finish_streaming(card_instance_id, fail_content)
        except Exception:
            logger.exception("'[CARD] fail_card error {}'", card_instance_id)
            return False
        return True

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
