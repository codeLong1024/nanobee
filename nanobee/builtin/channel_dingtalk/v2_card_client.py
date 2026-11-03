"""钉钉 v2 结构化卡片 REST 客户端 — 创建/投放/两步更新。

对齐官方连接器（dingtalk-openclaw-connector）``writeV2Card`` 的语义，
与 legacy 卡片（card_manager.py）同栈：复用 :class:`DingTalkCardClient`
的 token/headers/错误处理，QPS 由 :class:`RateLimiter` 令牌桶约束。

流程契约（官方实现 + 第一轮真机 demo 实证）：
- 创建：POST ``/card/instances``（v2 模板 + 初始 cardParamMap，
  callbackType=STREAM）→ POST ``/card/instances/deliver``
- 每帧更新两步：先 PUT ``/card/streaming``（key="content"，驱动打字机，
  终帧置空以关闭流式生命周期）→ 再 PUT ``/card/instances``（提交
  blockList/content/copy_content/flowStatus/hasAction/statusLine）
- 写入按卡片串行（官方 writeTail promise 链的 asyncio.Lock 等价物）；
  终态后拒绝迟到帧；QpsLimit 退避后重试一次
"""

from __future__ import annotations

import asyncio
import json
import random
import string
import time
from typing import Any

import httpx

from nanobee.builtin.channel_dingtalk.card_client import DingTalkCardClient
from nanobee.builtin.channel_dingtalk.rate_limiter import RateLimiter
from nanobee.builtin.channel_dingtalk.v2_card_renderer import (
    V2_CARD_TEMPLATE_ID,
    V2CardFrame,
)
from nanobee.utils.logger import logger

__all__ = ["DingTalkV2CardClient"]


class DingTalkV2CardClient:
    """单张 v2 结构化卡片的客户端（创建 → 逐帧推送 → 终态）。

    一个实例对应一张卡片（out_track_id 在构造时生成）；多会话各自持有
    实例，共享外部的 :class:`RateLimiter`（全局令牌桶）。
    """

    def __init__(
        self,
        card_client: DingTalkCardClient,
        robot_code: str,
        target: dict[str, str],
        *,
        template_id: str = V2_CARD_TEMPLATE_ID,
        rate_limiter: RateLimiter | None = None,
        out_track_id: str | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            card_client: 复用的卡片 HTTP 客户端（token/headers 设施）。
            robot_code: 机器人 robotCode（投放目标标识）。
            target: 投放目标，``{"receiverUserId": ...}``（单聊）或
                ``{"openConversationId": ...}``（群聊）。
            template_id: v2 卡片模板 ID（默认官方公共模板）。
            rate_limiter: 外部共享的 QPS 令牌桶；缺省时自建。
            out_track_id: 卡片实例 ID（测试注入用；缺省自动生成）。
        """
        self._client = card_client
        self._robot_code = robot_code
        self._target = target
        self._template_id = template_id
        self._rate_limiter = rate_limiter or RateLimiter(max_qps=20)
        self._write_lock = asyncio.Lock()
        self._terminal = False
        self.out_track_id = out_track_id or (
            f"card_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"
        )

    # ------------------------------------------------------------------
    # 卡片生命周期
    # ------------------------------------------------------------------

    async def create(self, initial_param_map: dict[str, str]) -> str:
        """创建并投放卡片。

        Args:
            initial_param_map: 渲染器产出的初始 cardParamMap。

        Returns:
            卡片实例 ID（outTrackId）。

        Raises:
            RuntimeError: 创建或投放被服务端拒绝。
        """
        client = await self._client.ensure_async_client()
        headers = await self._client.get_headers_async()

        create_body: dict[str, Any] = {
            "cardTemplateId": self._template_id,
            "outTrackId": self.out_track_id,
            "cardData": {"cardParamMap": initial_param_map},
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": True},
            "imRobotOpenSpaceModel": {"supportForward": True},
        }
        await self._put_with_retry(
            client, "POST", f"{self._client.api_url}/card/instances", headers, create_body
        )

        deliver_body = self._build_deliver_body()
        resp = await self._put_with_retry(
            client, "POST", f"{self._client.api_url}/card/instances/deliver", headers, deliver_body
        )
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"v2 卡片投放被拒绝: {data}")
        logger.info("[V2CARD] 创建+投放成功 card={}", self.out_track_id)
        return self.out_track_id

    async def push(self, frame: V2CardFrame) -> bool:
        """推送一帧更新（两步式，写入串行化）。

        Args:
            frame: 渲染器产出的帧快照。

        Returns:
            True=已推送；False=终态后迟到帧被拒绝。
        """
        if self._terminal:
            logger.warning("[V2CARD] 卡片已终态，忽略迟到帧 card={}", self.out_track_id)
            return False
        async with self._write_lock:
            if self._terminal:
                return False
            client = await self._client.ensure_async_client()
            headers = await self._client.get_headers_async()
            # 第一步：streaming 通道（key=content，终帧置空关闭打字机）。
            # 终态容忍（官方 0.8.27 writeV2Card 语义）：关闭流式失败仍继续提交
            # instances 终态，避免卡片永久停留在转圈态；非终态帧照常抛出。
            try:
                await self._put_with_retry(
                    client, "PUT", f"{self._client.api_url}/card/streaming", headers,
                    {
                        "outTrackId": self.out_track_id,
                        "guid": self._guid(),
                        "key": "content",
                        "content": frame.streaming_content,
                        "isFull": True,
                        "isFinalize": frame.finished,
                        "isError": False,
                    },
                )
            except httpx.HTTPError:
                if not frame.finished:
                    raise
                logger.warning(
                    "[V2CARD] 终态帧 streaming 关闭失败，仍提交 instances 终态 card={}",
                    self.out_track_id,
                )
            # 第二步：提交结构化变量
            await self._put_with_retry(
                client, "PUT", f"{self._client.api_url}/card/instances", headers,
                {
                    "outTrackId": self.out_track_id,
                    "cardData": {"cardParamMap": frame.card_param_map},
                    "cardUpdateOptions": {"updateCardDataByKey": True},
                },
            )
            if frame.finished:
                self._terminal = True
                logger.info(
                    "[V2CARD] 终态已推送 card={} flowStatus={} steps={}",
                    self.out_track_id,
                    frame.card_param_map.get("flowStatus"),
                    len(json.loads(frame.card_param_map["blockList"])),
                )
            else:
                logger.debug(
                    "[V2CARD] 帧已推送 card={} streaming_len={} steps={}",
                    self.out_track_id,
                    len(frame.streaming_content),
                    len(json.loads(frame.card_param_map["blockList"])),
                )
            return True

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _put_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        """带令牌桶限流的请求；QpsLimit 退避后重试一次。

        Args:
            client: HTTP 客户端。
            method: HTTP 方法（POST/PUT）。
            url: 完整 URL。
            headers: 请求头（含 token）。
            body: 请求体。

        Returns:
            成功的响应对象。

        Raises:
            httpx.HTTPStatusError: 重试后仍失败。
        """
        async with self._rate_limiter:
            resp = await client.request(method, url, headers=headers, json=body)
        if resp.status_code == 403 and "QpsLimit" in resp.text:
            logger.warning("[V2CARD] QPS 限流，退避重试 card={} url={}", self.out_track_id, url)
            await asyncio.sleep(2.0)
            async with self._rate_limiter:
                resp = await client.request(method, url, headers=headers, json=body)
        await self._client.check_response(resp, f"[v2:{self.out_track_id}] {method} {url}")
        resp.raise_for_status()
        return resp

    def _build_deliver_body(self) -> dict[str, Any]:
        """构造投放请求体（群聊/单聊两种 openSpaceId，与 legacy 卡片一致）。"""
        receiver_user_id = self._target.get("receiverUserId", "")
        if receiver_user_id:
            return {
                "outTrackId": self.out_track_id,
                "userIdType": 1,
                "openSpaceId": f"dtv1.card//IM_ROBOT.{receiver_user_id}",
                "imRobotOpenDeliverModel": {
                    "spaceType": "IM_ROBOT",
                    "robotCode": self._robot_code,
                    "extension": {"dynamicSummary": "true"},
                },
            }
        return {
            "outTrackId": self.out_track_id,
            "userIdType": 1,
            "openSpaceId": f"dtv1.card//IM_GROUP.{self._target['openConversationId']}",
            "imGroupOpenDeliverModel": {"robotCode": self._robot_code},
        }

    @staticmethod
    def _guid() -> str:
        """生成一次流式更新的 guid（官方契约：时间戳_随机串）。"""
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{int(time.time() * 1000)}_{suffix}"
