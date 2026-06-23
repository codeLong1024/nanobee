"""DingTalk message sending and media handling.

This module handles:
- Sending markdown text messages
- Sending media (images, files, videos)
- Media upload to DingTalk
- Remote media fetching with SSRF protection
- File download from DingTalk
- AI Card streaming (typing effect via /card/streaming)
"""

from __future__ import annotations

import json
import mimetypes
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .media.download import download_dingtalk_file
from .media.fetch import read_media_bytes
from .media.helpers import (
    guess_filename,
    guess_upload_type,
    is_http_url,
    normalize_upload_payload,
)
from .media.image_processor import process_local_images, process_bare_image_paths
from .media.markers import (
    process_audio_markers,
    process_video_markers,
    upload_and_replace_file_markers,
)
from .media.raw_path import process_raw_media_paths
from .media.upload import upload_media
from .session import is_group_session, parse_group_session
from .token import TokenManager

from .emotion_handler import recall_thinking_emoji as _recall_thinking_emoji
from .emotion_hook import DingTalkEmotionHook, EmotionContext

from nanobee.utils.logger import logger



class DingTalkSender:
    """Handles sending messages and media via DingTalk API.

    Thin orchestrator that delegates to specialized modules:
    - :mod:`.media.helpers` for extension/upload helpers
    - :mod:`.media.fetch` for remote media fetching
    - :mod:`.media.download` for file download from DingTalk
    - :mod:`.media.upload` for media upload to DingTalk
    - :mod:`.token` for access token management

    Streaming (AI Card):
    - Manages AI Card lifecycle for agent streaming output
    - 5-path send: streaming delta / streaming end / progress skip /
      non-streaming w/ card / markdown fallback
    """

    # Cap for _streamed_chats to prevent unbounded memory growth
    _STREAMED_CHAT_MAX = 5000

    def __init__(
        self,
        config: Any,
        logger: Any,
        http_client: httpx.AsyncClient | None = None,
        card_manager: Any = None,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
    ):
        self.config = config
        self.logger = logger
        self._http = http_client
        self._card_manager = card_manager
        self._token_manager = TokenManager(
            client_id=config.client_id,
            client_secret=config.client_secret,
            http=http_client,
            logger=logger,
            token_provider=token_provider,
        )

        # Streaming state keyed by card_id (每个 card_id 天然唯一，无并发碰撞)
        self._streaming_buffers: dict[str, str] = {}  # card_id → accumulated content
        self._overflow_cards: set[str] = set()  # card_id set：已溢出，停止累加
        self._streamed_cards: OrderedDict[str, bool] = OrderedDict()  # LRU, bounded
        self._card_has_streamed: set[str] = set()  # card_id set
        # Emotion context keyed by chat_id（单聊/群聊共用，emotion 是次要视觉反馈）
        self._emotion_contexts: dict[str, EmotionContext] = {}

    def setup(self, http_client: httpx.AsyncClient, card_manager: Any = None) -> None:
        """Configure HTTP client and card manager after construction."""
        self._http = http_client
        self._token_manager.http = http_client
        if card_manager is not None:
            self._card_manager = card_manager

    async def read_media_bytes(self, media_ref: str, **kwargs: Any) -> tuple[bytes | None, str | None, str | None]:
        """Read media bytes from URL or local file. Delegates to :func:`media.fetch.read_media_bytes`."""
        return await read_media_bytes(self._http, media_ref, self.logger, **kwargs)

    def set_token_provider(self, provider: Callable[[], Awaitable[str | None]]) -> None:
        """设置共享 Token 提供者（如 DingTalkAPI.get_access_token），消除重复 HTTP 调用。"""
        self._token_manager.token_provider = provider

    async def close(self) -> None:
        """Release HTTP client reference."""
        self._http = None

    # ==================== Token Management ====================

    async def get_access_token(self) -> str | None:
        """Get or refresh Access Token — delegates to TokenManager."""
        return await self._token_manager.get_access_token()

    # ==================== File Download from DingTalk ====================

    async def download_dingtalk_file(
        self,
        download_code: str,
        filename: str,
        sender_id: str,
        *,
        retries: int = 2,
        connect_timeout: float = 15.0,
        read_timeout: float = 120.0,
    ) -> str | None:
        """Download a DingTalk file to the media directory, return local path.

        Delegates to :func:`media.download.download_dingtalk_file`.
        """
        token = await self.get_access_token()
        return await download_dingtalk_file(
            http=self._http,
            token=token,
            client_id=self.config.client_id,
            download_code=download_code,
            filename=filename,
            sender_id=sender_id,
            logger=self.logger,
            retries=retries,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    # ==================== Media Upload ====================

    async def upload_media(
        self,
        token: str,
        data: bytes,
        media_type: str,
        filename: str,
        content_type: str | None,
        agent_id: str | None = None,
    ) -> str | None:
        """Upload media to DingTalk and return media_id.

        Delegates to :func:`media.upload.upload_media`.
        """
        return await upload_media(
            http=self._http,
            token=token,
            data=data,
            media_type=media_type,
            filename=filename,
            content_type=content_type,
            logger=self.logger,
            agent_id=agent_id or self.config.client_id,
        )

    # ==================== Message Sending ====================

    def _is_raw_group_id(self, chat_id: str) -> bool:
        """Check if chat_id is a raw DingTalk openConversationId (group)."""
        return chat_id.startswith("cid/")

    async def _send_batch_message(
        self,
        token: str,
        chat_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
        card_data: dict[str, Any] | None = None,
        sender_staff_id: str | None = None,
    ) -> bool:
        """Send a batch message (private or group).

        For ``sampleCardMsg``, the actual card JSON goes in ``card_data.cardJson``
        and ``msg_param`` should be ``{}``.  For other types (e.g. ``sampleMarkdown``)
        the content lives in ``msg_param`` and ``card_data`` is unused.

        Args:
            sender_staff_id: DingTalk staff ID for private chat routing.
                If provided and not empty, used as ``userIds`` for batchSend API.
                Falls back to ``chat_id`` if ``None`` or empty.
        """
        headers = {"x-acs-dingtalk-access-token": token}
        if is_group_session(chat_id) or self._is_raw_group_id(chat_id):
            if self._is_raw_group_id(chat_id):
                conversation_id = chat_id
            else:
                _, conversation_id = parse_group_session(chat_id)
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload: dict[str, Any] = {
                "robotCode": self.config.client_id,
                "openConversationId": conversation_id or chat_id,
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
            if card_data is not None:
                payload["cardData"] = card_data
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            # Use sender_staff_id if available, fallback to chat_id
            user_id = sender_staff_id if sender_staff_id else chat_id
            payload = {
                "robotCode": self.config.client_id,
                "userIds": [user_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
            if card_data is not None:
                payload["cardData"] = card_data

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            body = resp.text
            if resp.status_code != 200:
                self.logger.error("'send failed msgKey={} status={} body={}'", msg_key, resp.status_code, body[:500])
                return False
            try:
                result = resp.json()
            except Exception:
                result = {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                self.logger.error("'send api error msgKey={} errcode={} body={}'", msg_key, errcode, body[:500])
                return False
            self.logger.debug("'message sent to {} with msgKey={}'", chat_id, msg_key)
            return True
        except httpx.TransportError:
            self.logger.exception("'network error sending message msgKey={}'", msg_key)
            return False
        except Exception:
            self.logger.exception("'Error sending message msgKey={}'", msg_key)
            return False

    async def _send_markdown_text(self, token: str, chat_id: str, content: str,
                                   sender_staff_id: str | None = None) -> bool:
        """Send markdown text message."""
        title = getattr(self.config, 'markdown_title', '智能体回复')
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleMarkdown",
            {"text": content, "title": title},
            sender_staff_id=sender_staff_id,
        )

    async def _send_media_ref(self, token: str, chat_id: str, media_ref: str,
                               sender_staff_id: str | None = None) -> bool:
        """Send a media reference (URL or local file)."""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return True

        self.logger.debug("[MEDIA] Sending media ref: chat_id={}, ref={}, sender_staff_id={}",
                          chat_id, media_ref, sender_staff_id)
        upload_type = guess_upload_type(media_ref)

        # Try sending image URL directly
        if upload_type == "image" and is_http_url(media_ref):
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_ref},
                sender_staff_id=sender_staff_id,
            )
            if ok:
                return True
            self.logger.warning("'image url send failed, trying upload fallback: {}'", media_ref)

        # Read and upload media
        data, filename, content_type = await read_media_bytes(self._http, media_ref, self.logger)
        if not data:
            self.logger.error("'media read failed: {}'", media_ref)
            return False

        filename = filename or guess_filename(media_ref, upload_type)
        data, filename, content_type = normalize_upload_payload(filename, data, content_type, self.logger)
        file_type = Path(filename).suffix.lower().lstrip(".")
        if not file_type:
            guessed = mimetypes.guess_extension(content_type or "")
            file_type = (guessed or ".bin").lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"

        media_id = await self.upload_media(
            token=token,
            data=data,
            media_type=upload_type,
            filename=filename,
            content_type=content_type,
        )
        if not media_id:
            return False

        # Send image with media_id
        if upload_type == "image":
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_id},
                sender_staff_id=sender_staff_id,
            )
            if ok:
                return True
            self.logger.warning("'image media_id send failed, falling back to file: {}'", media_ref)

        # Send as file
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleFile",
            {"mediaId": media_id, "fileName": filename, "fileType": file_type},
            sender_staff_id=sender_staff_id,
        )

    async def _send_msg_media_refs(
        self, token: str, chat_id: str, media_refs: list[str],
        sender_staff_id: str | None = None,
    ) -> None:
        """Send a list of media references as native DingTalk file messages.

        Each ref is processed by :meth:`_send_media_ref`; failures are logged
        and a fallback text message is sent instead.

        Args:
            sender_staff_id: Passed through to _send_batch_message for
                private chat user ID routing.
        """
        for media_ref in media_refs:
            ok = await self._send_media_ref(token, chat_id, media_ref, sender_staff_id=sender_staff_id)
            if ok:
                continue
            self.logger.error("'media send failed for {}'", media_ref)
            filename = guess_filename(media_ref, guess_upload_type(media_ref))
            await self._send_markdown_text(
                token, chat_id,
                f"[Attachment send failed: {filename}]",
                sender_staff_id=sender_staff_id,
            )

    async def send(self, msg: Any) -> None:
        """Send an outbound message.

        Five paths:
        1. **Streaming delta** (``_stream_delta``): accumulate content
           and push to AI Card via ``card_manager.stream_content()`` — typing effect.
        2. **Streaming end** (``_stream_end``, no resume): final push + ``finish_streaming()``.
        3. **Progress/status messages** (``_progress``, ``_retry_wait``): silently
           skipped when a card is pending — they must NOT pop the card.
        4. **Non-streaming with card**: pop and finalize/fail the card.
        5. **Non-streaming without card**: fallback to markdown (and optional media).

        **Media pipeline** (applied to non-streaming content before sending):
        1. ``process_local_images()``      — replace Markdown image paths
        2. ``process_bare_image_paths()``  — handle bare image paths
        3. ``process_video_markers()``     — process [DINGTALK_VIDEO]
        4. ``process_audio_markers()``     — process [DINGTALK_AUDIO]
        5. ``upload_and_replace_file_markers()`` — process [DINGTALK_FILE]
        6. ``process_raw_media_paths()``   — handle remaining bare paths (safety net)
        7. Send cleaned text via card/markdown
        8. Send media references via native DingTalk API
        """
        token = await self.get_access_token()
        if not token:
            return

        metadata = msg.metadata or {}
        chat_id = msg.chat_id
        sender_staff_id = getattr(msg, "sender_staff_id", None) or metadata.get("sender_staff_id")
        # card_id 由回调闭包嵌入 metadata，每个 card 天然唯一，无并发碰撞
        card_id = metadata.get("_card_id")
        if metadata.get("_stream_end"):
            self.logger.debug("[PROTO] stream_end event for chat={} (resuming={})",
                              chat_id, metadata.get("_resuming"))
        elif not metadata.get("_stream_delta"):
            self.logger.debug("[SEND] msg to chat={}, content_len={}, media={}, sender_staff_id={}",
                              chat_id, len(msg.content or ""),
                              getattr(msg, "media", []), sender_staff_id)

        # ============ Rich media preprocessing pipeline ============
        content = msg.content or ""
        if (
            not metadata.get("_stream_delta")
            and content
            and self.config.enable_marker_processing
        ):
            try:
                content = await process_local_images(content, self, token, self.logger)
                content = await process_bare_image_paths(content, self, token, self.logger)
                content = await process_video_markers(
                    content, self._http, token, self, chat_id, self.logger,
                )
                content = await process_audio_markers(
                    content, self._http, token, self, chat_id, self.logger,
                )
                content = await upload_and_replace_file_markers(
                    content, self, token, chat_id, self.logger,
                )
                content = await process_raw_media_paths(
                    content, self, token, chat_id, self.logger,
                )
            except Exception:
                self.logger.exception("media pipeline error, continuing with original content")

        # --- 1. Streaming delta: accumulate + push to card ---
        if card_id and metadata.get("_stream_delta"):
            # 已溢出的卡片，直接丢弃后续增量（避免无限截断循环）
            if card_id in self._overflow_cards:
                return

            if card_id not in self._streaming_buffers:
                self._streaming_buffers[card_id] = ""
                await self._trigger_emotion(chat_id, "writing")
            prev = self._streaming_buffers[card_id]
            delta = msg.content or ""
            buffer_max = getattr(self.config, "stream_buffer_max_chars", 100_000) or 100_000
            if len(prev) + len(delta) > buffer_max:
                self.logger.warning(
                    "[STREAM] Buffer overflow for chat={}, {}/{} chars, stopping accumulation",
                    chat_id, len(prev), buffer_max,
                )
                # 标记溢出：后续 delta 全部丢弃，_stream_end 时附加截断提示
                self._overflow_cards.add(card_id)
                room = max(0, buffer_max - len(prev))
                self._streaming_buffers[card_id] = prev + delta[:room]
                accumulated = self._streaming_buffers[card_id]
                if self._card_manager:
                    try:
                        await self._card_manager.stream_content(card_id, accumulated)
                        self._card_has_streamed.add(card_id)
                    except Exception:
                        self.logger.debug("[STREAM] stream_content error", exc_info=True)
                return

            self._streaming_buffers[card_id] = prev + delta
            accumulated = self._streaming_buffers[card_id]
            if self._card_manager:
                try:
                    await self._card_manager.stream_content(card_id, accumulated)
                    self._card_has_streamed.add(card_id)
                except Exception:
                    self.logger.debug("[STREAM] stream_content error", exc_info=True)
            return

        # --- 2. Streaming end: finalize card ---
        if card_id and metadata.get("_stream_end"):
            if metadata.get("_resuming"):
                await self._trigger_emotion(chat_id, "tool")
                self._streaming_buffers.pop(card_id, None)
                self._overflow_cards.discard(card_id)  # 清除溢出标记，下个流式段重新开始
                return
            await self._trigger_emotion(chat_id, "done")
            accumulated = self._streaming_buffers.pop(card_id, "") or (msg.content or "")
            # 溢出的卡片附加截断提示
            if card_id in self._overflow_cards:
                self._overflow_cards.discard(card_id)
                accumulated += "\n\n---\n⚠️ 回复内容过长，已截断"
            if accumulated.strip() and self._card_manager:
                try:
                    await self._card_manager.stream_content(card_id, accumulated)
                    await self._card_manager.finish_streaming(card_id, accumulated)
                except Exception:
                    self.logger.warning("[STREAM] finish failed, falling back to markdown", exc_info=True)
                    if accumulated.strip():
                        try:
                            await self._send_markdown_text(token, chat_id, accumulated.strip(),
                                                           sender_staff_id=sender_staff_id)
                        except Exception:
                            self.logger.exception("[STREAM] fallback markdown also failed")
                    self._cleanup_chat_context(chat_id)
                    return
            else:
                if self._card_manager:
                    try:
                        await self._card_manager.finish_streaming(card_id, accumulated)
                    except Exception:
                        pass
            if msg.media:
                await self._send_msg_media_refs(token, chat_id, msg.media,
                                                sender_staff_id=sender_staff_id)
            self._mark_card_streamed(card_id)
            self._cleanup_chat_context(chat_id)
            return

        # --- 3. Progress / status messages: silently skip ---
        if metadata.get("_progress") or metadata.get("_retry_wait"):
            self.logger.debug(
                "'[SKIP] Skipping progress/status message for chat={}'", chat_id,
            )
            return

        # --- 4. Non-streaming: finalize card with content ---
        if card_id and self._card_manager and content and content.strip():
            self.logger.debug("'[CARD] Finalizing card {} for chat={}'", card_id, chat_id)
            if card_id in self._card_has_streamed:
                ok = True
                try:
                    await self._card_manager.finish_card_status(card_id)
                except Exception:
                    self.logger.exception("[CARD] finish_card_status failed, fallback to finalize_card")
                    ok = await self._card_manager.finalize_card(card_id, content.strip())
            else:
                ok = await self._card_manager.finalize_card(card_id, content.strip())
            await self._trigger_emotion(chat_id, "done")
            if ok:
                await self._send_msg_media_refs(token, chat_id, msg.media or [],
                                                sender_staff_id=sender_staff_id)
                self._mark_card_streamed(card_id)
                self._cleanup_chat_context(chat_id)
                return
            self.logger.warning("[CARD] finalize_card failed, falling back to markdown")
        elif card_id and msg.media:
            # 纯媒体推送（工具调用发图/文件），不消耗卡片
            await self._send_msg_media_refs(token, chat_id, msg.media,
                                            sender_staff_id=sender_staff_id)
            return

        # Skip markdown if streaming already delivered via card
        if card_id in self._streamed_cards:
            self._streamed_cards.pop(card_id, None)
            await self._send_msg_media_refs(token, chat_id, msg.media or [],
                                            sender_staff_id=sender_staff_id)
            return

        # Fall back to markdown
        if content and content.strip():
            self.logger.info("'[SEND] Markdown to chat={} ({} chars)'", chat_id, len(content))
            await self._send_markdown_text(token, chat_id, content.strip(),
                                           sender_staff_id=sender_staff_id)

        await self._send_msg_media_refs(token, chat_id, msg.media or [],
                                        sender_staff_id=sender_staff_id)
        await self._recall_emotion(chat_id)

    async def send_via_card(
        self, token: str, chat_id: str, content: str,
        sender_staff_id: str | None = None,
    ) -> bool:
        """通过 AI Card 一键投递完整内容（创建→打字效果→关闭）。

        用于子代理结果等异步触发的消息，为 DingTalk 用户提供 Card UI 体验。
        Card 创建失败或 card_manager 不可用时返回 False，调用方应回退到 markdown。

        Args:
            token: DingTalk Access Token。
            chat_id: DingTalk 会话 ID（staff_id 或 openConversationId）。
            content: 要投递的 Markdown 内容。
            sender_staff_id: 发送者 staff_id（可选，用于私聊路由，缺失时用 chat_id）。

        Returns:
            True 表示 Card 创建并投递成功。
        """
        if not self._card_manager or not content or not content.strip():
            return False
        try:
            from nanobee.builtin.channel_dingtalk.card_manager import CardManager
            target_id = sender_staff_id or chat_id
            track_id = CardManager.generate_track_id()
            card_id = await self._card_manager.create_card(
                card_instance_id=track_id,
                robot_code=self.config.client_id,
                target={"receiverUserId": target_id},
            )
            if not card_id:
                return False
            # 打字效果：启动流式模式 → 推送完整内容 → 关闭卡片
            await self._card_manager.start_streaming(card_id)
            await self._card_manager.stream_content(card_id, content.strip())
            await self._card_manager.finish_streaming(card_id, content.strip())
            self._cleanup_chat_context(chat_id)
            return True
        except Exception:
            self.logger.exception("send_via_card failed for chat={}", chat_id)
            return False

    # ------------------------------------------------------------------
    # Streaming card tracking (bounded LRU via OrderedDict)
    # ------------------------------------------------------------------

    def is_card_handled_by_streaming(self, card_id: str) -> bool:
        """检查给定 card 是否已通过流式路径处理完成。

        流式路径处理的 card 满足下列条件之一：
        1. 有内容通过流式 delta 推送到卡片（_card_has_streamed）
        2. 卡片已通过 _stream_end 事件最终化（_streamed_cards）

        channel 侧使用此方法判断是否应将响应交给 sender 兜底发送：
        已通过流式处理 → 跳过（卡片已送达内容）；未处理 → 走 sender 兜底。
        """
        return card_id in self._card_has_streamed or card_id in self._streamed_cards

    async def finalize_card_with_notification(
        self, card_id: str, chat_id: str, notification: str,
    ) -> None:
        """最终化流式卡片，在现有内容后追加通知。

        用于 max_iterations 中断流式时，保留卡片已有碎片内容，
        追加分割线和超限提示，不发 markdown。缓存区为空时仅设置
        卡片 FINISHED 状态（不追加）。

        Args:
            card_id: 卡片实例 ID
            chat_id: 聊天会话 ID（用于情感表情跟踪）
            notification: 要追加的通知文本（不含冗余前缀）
        """
        if not self._card_manager:
            return
        remaining = self._streaming_buffers.pop(card_id, None)
        if remaining is not None and remaining.strip():
            combined = remaining.strip() + "\n\n---\n⚠️ " + notification.strip()
            try:
                await self._card_manager.stream_content(card_id, combined)
                await self._card_manager.finish_streaming(card_id, combined)
            except Exception:
                self.logger.warning(
                    "[CARD] finalize_card_with_notification failed, fallback to status only",
                    exc_info=True,
                )
                try:
                    await self._card_manager.finish_card_status(card_id)
                except Exception:
                    self.logger.warning("[CARD] finish_card_status also failed")
        else:
            # 缓存区已空（on_stream_end(resuming=True) 清空后未产生新流式）。
            # 必须走 stream_content → finish_streaming 路径：
            # 仅调 finish_streaming 会通过 PUT /card/instances 设置 msgContent，
            # 但卡片内容此前是由 POST /card/streaming 推送的，丁丁后端将二者分开存储，
            # instance msgContent 不会覆盖 streaming content → 卡片仍显示旧碎片内容。
            if notification.strip():
                try:
                    await self._card_manager.stream_content(card_id, notification.strip())
                    await self._card_manager.finish_streaming(card_id, notification.strip())
                except Exception:
                    self.logger.warning(
                        "[CARD] finalize_card_notification stream_content+finish failed, "
                        "fallback to finish_card_status",
                        exc_info=True,
                    )
                    try:
                        await self._card_manager.finish_card_status(card_id)
                    except Exception:
                        self.logger.warning("[CARD] finish_card_status also failed")
            else:
                try:
                    await self._card_manager.finish_card_status(card_id)
                except Exception:
                    self.logger.warning(
                        "[CARD] finish_card_status failed in finalize_card_with_notification",
                        exc_info=True,
                    )
        await self._trigger_emotion(chat_id, "done")
        self._mark_card_streamed(card_id)

    def _mark_card_streamed(self, card_id: str) -> None:
        """Record a card as having completed AI Card streaming, with LRU eviction."""
        self._streamed_cards[card_id] = True
        self._streamed_cards.move_to_end(card_id)
        while len(self._streamed_cards) > self._STREAMED_CHAT_MAX:
            self._streamed_cards.popitem(last=False)

    # ------------------------------------------------------------------
    # Per-chat emotion cleanup
    # ------------------------------------------------------------------

    def _cleanup_chat_context(self, chat_id: str) -> None:
        """Clean up per-chat emotion state after message processing."""
        self._emotion_contexts.pop(chat_id, None)

    # ------------------------------------------------------------------
    # Emotion-driven feedback (multi-status emoji)
    # ------------------------------------------------------------------

    async def _trigger_emotion(self, chat_id: str, state_name: str) -> None:
        """Update the DingTalk emotion for *chat_id* to *state_name*.

        Silently skips if no :class:`EmotionContext` exists (e.g. non-card
        reply path).  Exceptions are logged but never propagated.
        """
        ctx = self._emotion_contexts.get(chat_id)
        if ctx is None:
            return
        try:
            hook = DingTalkEmotionHook(ctx)
            await hook.update(state_name)
        except Exception:
            self.logger.exception(
                "[Emotion] Failed to update '{}' for chat={}'", state_name, chat_id,
            )

    async def _recall_emotion(self, chat_id: str) -> None:
        """Force-recall the DingTalk emotion for *chat_id* (non-streaming fallback).

        Used when the message flow ends without ever entering a streaming path,
        ensuring the initial 🤔 thinking emoji is always cleaned up.
        """
        ctx = self._emotion_contexts.get(chat_id)
        if ctx is None:
            return
        try:
            await _recall_thinking_emoji(
                ctx.http_client, ctx.token, ctx.robot_code,
                ctx.open_msg_id, ctx.open_conversation_id,
            )
        except Exception:
            self.logger.exception("'[Emotion] recall failed for chat={}'", chat_id)
        self._cleanup_chat_context(chat_id)


__all__ = ["DingTalkSender"]
