"""DingTalk message handling for nanobee — using ChatbotHandler (SDK).

Preserves AI Card creation, emotion feedback.
Message routing is adapted from nanobot's MessageBus → nanobee kernel.
"""

from __future__ import annotations

import asyncio
import json as _json
import time as _time
from dataclasses import dataclass, field
from typing import Any

from .auth import (
    AckMessage,
    ChatbotHandler,
    ChatbotMessage,
    CallbackMessage,
    DINGTALK_AVAILABLE,
)
from .card_manager import CardManager
from .emotion_handler import (
    add_thinking_emoji,
    recall_thinking_emoji,
)
from .emotion_hook import EmotionContext
from .media.file_parser import parse_file_content
from .session import build_session_key

from nanobee.utils.logger import logger



@dataclass
class ParsedMessage:
    """Lightweight data container for parsed message context."""
    content: str
    sender_id: str
    sender_name: str
    conversation_type: str | None
    conversation_id: str | None
    chat_id: str
    session_key: str
    msg_id: str
    raw_message: CallbackMessage
    media: list[str] = field(default_factory=list)
    sender_staff_id: str | None = None


class NanobeeDingTalkHandler(ChatbotHandler):
    """DingTalk Stream message handler for nanobee.

    Responsibilities:
    - Parse incoming messages (text, image, file, richText)
    - Enqueue for serial processing per conversation
    - Create AI Card, coordinate emoji feedback
    - Route to nanobee kernel via channel._on_message()
    """

    def __init__(self, channel: "DingTalkChannelPlugin"):  # noqa: F821
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """Quick parse + enqueue — returns ACK immediately."""
        try:
            self.channel.logger.debug(
                "[DEBUG RAW] message.data = {}",
                _json.dumps(message.data, ensure_ascii=False, indent=2, default=str),
            )
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            content, file_paths = await self._extract_message_content(
                chatbot_msg, message,
            )

            if not content:
                self.channel.logger.warning(
                    "Received empty or unsupported message type: {}",
                    chatbot_msg.message_type,
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            conversation_type = message.data.get("conversationType")
            conversation_id = (
                message.data.get("conversationId")
                or message.data.get("openConversationId")
            )
            chat_id = conversation_id if conversation_type == "2" else sender_id
            session_key = build_session_key(sender_id, conversation_type, conversation_id)
            msg_id = (
                getattr(chatbot_msg, "message_id", "")
                or message.data.get("messageId", "")
            )

            self.channel.logger.info(
                "Received message from {} ({}): {}", sender_name, sender_id, content,
            )

            parsed = ParsedMessage(
                content=content,
                sender_id=sender_id,
                sender_staff_id=chatbot_msg.sender_staff_id,
                sender_name=sender_name,
                conversation_type=conversation_type,
                conversation_id=conversation_id,
                chat_id=chat_id,
                session_key=session_key,
                msg_id=msg_id,
                media=file_paths,
                raw_message=message,
            )

            asyncio.create_task(self._handle_message(parsed))

            return AckMessage.STATUS_OK, "OK"

        except Exception:
            self.channel.logger.exception("Error processing message")
            return AckMessage.STATUS_OK, "Error"

    async def _extract_message_content(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
    ) -> tuple[str, list[str]]:
        """Extract text content and file paths from an incoming message."""
        content = ""
        if chatbot_msg.text:
            content = chatbot_msg.text.content.strip()
        elif chatbot_msg.extensions.get("content", {}).get("recognition"):
            content = chatbot_msg.extensions["content"]["recognition"].strip()
        if not content:
            content = message.data.get("text", {}).get("content", "").strip()

        file_paths: list[str] = []
        msg_type = chatbot_msg.message_type

        # Pre-compute sender_id to avoid closure leakage
        sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"

        if msg_type == "picture" and chatbot_msg.image_content:
            content, file_paths = await self._handle_picture(
                chatbot_msg, message, sender_id, content,
            )
        elif msg_type == "audio":
            content, file_paths = await self._handle_audio(
                chatbot_msg, message, sender_id, content,
            )
        elif msg_type == "file":
            content, file_paths = await self._handle_file(
                chatbot_msg, message, sender_id, content,
            )
        elif msg_type == "video":
            content, file_paths = await self._handle_video(
                chatbot_msg, message, sender_id, content,
            )
        elif msg_type == "richText" and chatbot_msg.rich_text_content:
            content, file_paths = await self._handle_rich_text(
                chatbot_msg, message, sender_id, content,
            )

        if getattr(self.channel.config, "enable_file_parsing", False) and file_paths:
            for fp in list(file_paths):
                parsed = await parse_file_content(fp)
                if parsed:
                    max_chars = getattr(self.channel.config, "max_file_parse_chars", 2000)
                    snippet = parsed.text[:max_chars]
                    self.channel.logger.info(
                        "Parsed file content: format={} size={} chars={}",
                        parsed.format, parsed.file_size, len(parsed.text),
                    )
                    content = f"{content}\n\n[File content: {parsed.file_name}]\n{snippet}"

        return content, file_paths

    async def _handle_picture(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
        sender_id: str,
        content: str,
    ) -> tuple[str, list[str]]:
        """Handle picture message type."""
        file_paths: list[str] = []
        download_code = chatbot_msg.image_content.download_code
        if download_code:
            fp = await self.channel.sender.download_dingtalk_file(
                download_code, "image.jpg", sender_id,
            )
            if fp:
                file_paths.append(fp)
                content = content or "[Image]"
        return content, file_paths

    async def _handle_audio(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
        sender_id: str,
        content: str,
    ) -> tuple[str, list[str]]:
        """Handle audio message type."""
        file_paths: list[str] = []
        recognition = (
            message.data.get("content", {}).get("recognition", "")
            or chatbot_msg.extensions.get("content", {}).get("recognition", "")
        )
        if recognition:
            content = recognition.strip()
        download_code = message.data.get("downloadCode", "")
        if download_code:
            fp = await self.channel.sender.download_dingtalk_file(
                download_code, f"voice_{int(_time.time())}.amr", sender_id,
            )
            if fp:
                file_paths.append(fp)
        return content, file_paths

    async def _handle_file(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
        sender_id: str,
        content: str,
    ) -> tuple[str, list[str]]:
        """Handle file message type."""
        file_paths: list[str] = []
        download_code = (
            message.data.get("content", {}).get("downloadCode")
            or message.data.get("downloadCode")
        )
        fname = (
            message.data.get("content", {}).get("fileName")
            or message.data.get("fileName")
            or "file"
        )
        if download_code:
            fp = await self.channel.sender.download_dingtalk_file(
                download_code, fname, sender_id,
            )
            if fp:
                file_paths.append(fp)
                content = content or "[File]"
        return content, file_paths

    async def _handle_video(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
        sender_id: str,
        content: str,
    ) -> tuple[str, list[str]]:
        """Handle video message type."""
        file_paths: list[str] = []
        content = content or "[Video]"
        download_code = message.data.get("downloadCode", "")
        if download_code:
            fp = await self.channel.sender.download_dingtalk_file(
                download_code, f"video_{int(_time.time())}.mp4", sender_id,
            )
            if fp:
                file_paths.append(fp)
        return content, file_paths

    async def _handle_rich_text(
        self,
        chatbot_msg: ChatbotMessage,
        message: CallbackMessage,
        sender_id: str,
        content: str,
    ) -> tuple[str, list[str]]:
        """Handle richText message type."""
        file_paths: list[str] = []
        rich_list = chatbot_msg.rich_text_content.rich_text_list or []
        for item in rich_list:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                t = item.get("text", "").strip()
                if t:
                    content = (content + " " + t).strip() if content else t
            elif item.get("downloadCode"):
                dc = item["downloadCode"]
                fname = item.get("fileName") or "file"
                fp = await self.channel.sender.download_dingtalk_file(
                    dc, fname, sender_id,
                )
                if fp:
                    file_paths.append(fp)
                    content = content or "[File]"
        return content, file_paths

    async def _handle_message(self, parsed: "ParsedMessage") -> None:
        """Process a single message via nanobee kernel.

        Flow:
        1. Add thinking emoji
        2. Create AI Card
        3. Start streaming (shows "思考中...")
        4. Route to nanobee kernel — streaming updates via EventBus
        5. On error: fail_card + recall emoji
        """
        config = self.channel.config
        if config is None:
            return

        robot_code = self.channel._get_robot_code()
        msg_id = parsed.msg_id
        http = self.channel._http

        raw_data = getattr(parsed.raw_message, "data", {}) or {}
        open_conv_id = (
            raw_data.get("openConversationId")
            if isinstance(raw_data, dict)
            else None
        ) or parsed.conversation_id or ""

        # Pass the token provider callable (not a pre-resolved string)
        # so that retry attempts can fetch fresh tokens.
        token_provider = (
            self.channel.sender.get_access_token
            if self.channel.sender
            else None
        )

        if http and token_provider:
            await add_thinking_emoji(
                http, token_provider, robot_code, msg_id, open_conv_id,
            )

        emotion_ctx = EmotionContext(
            http_client=http,
            token=token_provider,
            robot_code=robot_code,
            open_msg_id=msg_id,
            open_conversation_id=open_conv_id,
        ) if http and token_provider else None
        if emotion_ctx and self.channel.sender:
            self.channel.sender._emotion_contexts[parsed.msg_id] = emotion_ctx

        card_instance_id: str | None = None
        card_manager = self.channel.card_manager
        if card_manager and http and token_provider and config.streaming:
            try:
                is_group = parsed.conversation_type == "2"
                target = (
                    {"openConversationId": open_conv_id}
                    if is_group
                    else {"receiverUserId": parsed.sender_id}
                )
                track_id = CardManager.generate_track_id()
                cid = await card_manager.create_card(
                    card_instance_id=track_id,
                    robot_code=robot_code,
                    target=target,
                )
                card_instance_id = cid
                self.channel.logger.info(
                    "[CARD] Created AI Card {} for chat={}", cid, parsed.chat_id,
                )
                await card_manager.start_streaming(cid, "思考中...")
            except Exception as e:
                self.channel.logger.warning("[CARD] AI Card setup failed: {}", e)

        try:
            is_dm = parsed.conversation_type != "2"
            async with self.channel.rate_limiter:
                await self.channel._on_message(
                    content=parsed.content,
                    sender_id=parsed.sender_id,
                    sender_staff_id=parsed.sender_staff_id,
                    sender_name=parsed.sender_name or "Unknown",
                    chat_id=parsed.chat_id,
                    media=parsed.media,
                    is_dm=is_dm,
                    session_key=parsed.session_key,
                    card_id=card_instance_id,
                    msg_id=parsed.msg_id,
                )
            self.channel.logger.info(
                "[DISPATCH] Message dispatched to agent [user={}]", parsed.sender_id,
            )
        except Exception as e:
            self.channel.logger.exception("[ERROR] Processing message failed: {}", e)
            if card_manager and card_instance_id:
                try:
                    await card_manager.fail_card(card_instance_id, str(e))
                except Exception:
                    pass
            if http and token_provider:
                await recall_thinking_emoji(
                    http, token_provider, robot_code, msg_id, open_conv_id,
                )


__all__ = ["NanobeeDingTalkHandler", "ParsedMessage"]
