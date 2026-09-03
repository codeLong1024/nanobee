"""DingTalk Channel main class for nanobee.

Uses Stream SDK (WebSocket) for message receiving.
Uses HTTP API (via DingTalkSender) for sending messages.
"""

from __future__ import annotations

import asyncio
import os
import random
from types import SimpleNamespace
from typing import Any

import httpx

from nanobee.channel.base import ChannelPlugin
from nanobee.channel.message import OutboundMessage as BeeOutboundMessage
from .auth import (
    DINGTALK_AVAILABLE,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)
from .card_client import DingTalkCardClient
from .card_manager import CardManager
from .config import DingTalkConfig
from .message import NanobeeDingTalkHandler
from .rate_limiter import RateLimiter
from .sender import DingTalkSender

from nanobee.utils.logger import logger



class DingTalkChannelPlugin(ChannelPlugin):
    """DingTalk channel using Stream Mode (SDK) + AI Card streaming for nanobee."""

    name = "channel_dingtalk"
    display_name = "钉钉机器人"
    supports_streaming = True

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self.logger = logger
        self.dingtalk_config: DingTalkConfig | None = None
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None
        self._running = False

        # Rate limiter
        self.rate_limiter = RateLimiter(max_qps=20)

        # Card manager + card client (lazy — init in start())
        self.card_client: DingTalkCardClient | None = None
        self.card_manager: CardManager | None = None

        # Sender (setup() called in start())
        self.sender: DingTalkSender | None = None

    def _load_config(self) -> DingTalkConfig:
        """从配置加载 DingTalk 配置。

        配置优先级：
        1. YAML channels.channel_dingtalk 段
        2. 环境变量 DINGTALK_*
        3. plugin.toml config 段
        4. 默认值
        """
        cfg = {}

        # 1. 从 YAML channels 段读取
        channels_dict = getattr(self.kernel.config, "channels", {})
        channel_key = self.metadata.name  # "channel_dingtalk"
        channel_cfg = channels_dict.get(channel_key, {}) if channels_dict else {}
        if channel_cfg:
            cfg.update(channel_cfg)

        # 2. 环境变量覆盖
        if not cfg.get("client_id"):
            cfg["client_id"] = os.environ.get("DINGTALK_CLIENT_ID", "")
        if not cfg.get("client_secret"):
            cfg["client_secret"] = os.environ.get("DINGTALK_CLIENT_SECRET", "")

        # 3. plugin.toml config 段（最低优先级）
        for key in DingTalkConfig.model_fields:
            val = self.get_config(key, None)
            if val is not None and key not in cfg:
                cfg[key] = val

        return DingTalkConfig.model_validate(cfg)

    async def start(self) -> None:
        """Start the DingTalk bot via Stream SDK."""
        self.dingtalk_config = self._load_config()

        if not DINGTALK_AVAILABLE:
            self.logger.error("dingtalk-stream SDK not installed. Run: pip install dingtalk-stream")
            return

        if not self.dingtalk_config.client_id or not self.dingtalk_config.client_secret:
            self.logger.error("client_id and client_secret not configured")
            return

        self._running = True
        if self.dingtalk_config.proxy_url:
            self._http = httpx.AsyncClient(proxies=self.dingtalk_config.proxy_url)
            self.logger.info("HTTP client using proxy: {}", self.dingtalk_config.proxy_url)
        else:
            self._http = httpx.AsyncClient()

        # 创建 sender（先不传 card_manager，它此时还未初始化）
        self.sender = DingTalkSender(
            self.dingtalk_config, self.logger,
            http_client=self._http,
        )
        self.sender.setup(self._http)

        # 创建 card_client + card_manager（需要 sender 的 token 函数）
        self.card_client = DingTalkCardClient(
            access_token_fn=self.sender.get_access_token,
            proxy_url=self.dingtalk_config.proxy_url,
        )
        self.card_manager = CardManager(self.card_client)

        # 把 card_manager 回写给 sender
        self.sender.setup(self._http, self.card_manager)

        self.logger.info(
            "Initializing Stream Client with Client ID: {}...",
            self.dingtalk_config.client_id,
        )
        credential = Credential(self.dingtalk_config.client_id, self.dingtalk_config.client_secret)
        self._client = DingTalkStreamClient(credential)

        # Register handler
        handler = NanobeeDingTalkHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

        self.logger.info("bot started with Stream Mode")

        # 主循环：保持连接 + 自动重连（_booted 已在 boot() 中提前设置，不影响消息处理）
        while self._running:
            try:
                await self._client.start()
            except Exception as e:
                self.logger.warning("stream error: {}", e)
            if self._running:
                delay = 3 + random.uniform(0, 4)
                self.logger.info("Reconnecting stream in %.1f seconds...", delay)
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        """Stop the DingTalk bot."""
        self._running = False
        if self.card_client:
            await self.card_client.close()
        if self._http:
            await self._http.aclose()
            self._http = None
        if self.sender:
            await self.sender.close()

    async def _process_incoming(
        self,
        message: Any,
        context_manager: Any,
    ) -> list:
        """实现 ChannelPlugin 抽象方法。

        钉钉通道不通过 handle_incoming 入口路由消息；
        消息直接由 Stream SDK → NanobeeDingTalkHandler → _on_message → kernel。
        此方法仅用于满足抽象基类要求。
        """
        return []

    async def send(self, message: BeeOutboundMessage, context_id: str = "default") -> None:
        """Send a message through DingTalk."""
        if self.sender is None:
            return
        # Convert nanobee OutboundMessage → internal format
        internal_msg = SimpleNamespace(
            channel=message.channel,
            chat_id=message.chat_id,
            content=message.content,
            metadata=message.metadata,
            media=message.media,
        )
        await self.sender.send(internal_msg)

    async def _on_agent_outbound(self, data: dict) -> None:
        """覆盖基类：为子代理自动触发等内部消息创建 AI Card 投递。

        子代理完成后，_injector → enqueue_message → _dispatch → agent.outbound 事件
        到达此处。基类 `_on_agent_outbound` 走裸 markdown，本覆盖改为创建 AI Card
        （打字效果 + Card UI），失败时回退到基类 markdown。
        """
        if not isinstance(data, dict):
            return
        channel_name = data.get("channel", "")
        if channel_name != self.metadata.name:
            return
        chat_id = data.get("chat_id", "direct")
        content = data.get("content", "")
        if not content:
            return

        # 尝试通过 AI Card 投递
        if self.sender and content.strip():
            token = await self.sender.get_access_token()
            if token:
                try:
                    ok = await self.sender.send_via_card(token, chat_id, content.strip())
                    if ok:
                        return  # Card 投递成功，不发送 markdown
                except Exception:
                    self.logger.exception(
                        "_on_agent_outbound: send_via_card failed for chat={}", chat_id,
                    )

        # Card 不可用或失败，回退到基类裸 markdown
        await super()._on_agent_outbound(data)

    async def send_delta(
        self,
        delta: Any,
        context_id: str = "default",
    ) -> None:
        """Forward streaming deltas to DingTalkSender."""
        if self.sender is None:
            return
        internal_msg = SimpleNamespace(
            channel=self.name,
            chat_id=context_id.split(":", 1)[-1] if ":" in context_id else context_id,
            content=getattr(delta, "content", str(delta) if delta else ""),
            metadata={"_stream_delta": True},
            media=[],
        )
        await self.sender.send(internal_msg)

    async def send_reasoning_delta(
        self, reasoning: str, context_id: str = "default"
    ) -> None:
        """流式发送推理过程增量。"""
        pass  # DingTalk card 暂不支持推理流式

    async def send_reasoning_end(self, context_id: str = "default") -> None:
        """标记推理过程结束。"""
        pass

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        chat_id: str,
        media: list[str] | None = None,
        sender_staff_id: str | None = None,
        is_dm: bool = False,
        session_key: str | None = None,
        card_id: str | None = None,
        msg_id: str | None = None,
    ) -> None:
        """处理入站消息 — 路由到 nanobee 内核并投递响应。

        消息处理分为两步：
        1. 调用内核 handle_message（流式/非流式两种模式）
        2. 将响应投递到钉钉通道（5 种分支场景）
        """
        try:
            self.logger.info("inbound: {} from {}", content, sender_name)

            context_id = f"dingtalk:{chat_id}"

            if self.kernel is None:
                self.logger.warning("内核未就绪，无法处理钉钉消息")
                return

            use_streaming = self.dingtalk_config is not None and self.dingtalk_config.streaming and self.sender is not None

            # 1. 调用内核
            if use_streaming:
                response = await self.kernel.handle_message(
                    str(content), context_id,
                    channel=self.metadata.name,
                    on_stream=self._make_stream_callback(chat_id, card_id=card_id, msg_id=msg_id),
                    on_stream_end=self._make_stream_end_callback(chat_id, card_id=card_id, msg_id=msg_id),
                    on_progress=self._make_progress_callback(chat_id, msg_id=msg_id),
                    sender_id=sender_id,
                    session_id=f"dingtalk:{chat_id}",
                    metadata={
                        "sender_staff_id": sender_staff_id,
                        "sender_name": sender_name,
                        "session_key": session_key,
                        "is_dm": is_dm,
                        "msg_id": msg_id,
                    },
                )
            else:
                response = await self.kernel.handle_message(
                    str(content), context_id,
                    channel=self.metadata.name,
                    on_progress=self._make_progress_callback(chat_id, msg_id=msg_id),
                    sender_id=sender_id,
                    session_id=f"dingtalk:{chat_id}",
                    metadata={
                        "sender_staff_id": sender_staff_id,
                        "sender_name": sender_name,
                        "session_key": session_key,
                        "is_dm": is_dm,
                        "msg_id": msg_id,
                    },
                )

            # 2. 投递响应
            await self._deliver_response(response, chat_id, card_id, msg_id, use_streaming, media)
        except Exception:
            self.logger.exception("Error processing DingTalk message")
            await self._fail_pending_card(card_id)

    # ------------------------------------------------------------------
    # 响应投递 — 5 种分支场景
    # ------------------------------------------------------------------

    @staticmethod
    def _build_resp_metadata(card_id: str | None, msg_id: str | None) -> dict[str, str]:
        """构造响应 metadata，嵌入 card_id 和 msg_id 供 sender 使用。"""
        metadata: dict[str, str] = {}
        if card_id:
            metadata["_card_id"] = card_id
        if msg_id:
            metadata["msg_id"] = msg_id
        return metadata

    async def _deliver_response(
        self,
        response: Any,
        chat_id: str,
        card_id: str | None,
        msg_id: str | None,
        use_streaming: bool,
        media: list[str] | None,
    ) -> None:
        """投递内核响应到钉钉通道。

        6 种场景，用 guard clause 扁平化处理：
        S) 系统通知（命令响应 / 错误通知）→ _deliver_system_notification（不走卡片流式）
        E) 非流式 → sender.send(content + media)
        D) 流式 + 无 card → markdown fallback sender.send
        C) 流式 + card + 未被流式处理 → sender.send(content + media)
        A) 流式 + card + 已处理 + max_iterations → finalize_card + media
        B) 流式 + card + 已处理 + 正常完成 → 仅 media（内容已在流式中送达）
        """
        if not response or not self.sender:
            return

        # 系统通知（命令响应 / 错误通知）：不走 LLM 卡片流式路径。
        # 错误场景在 4.3 提前返回，卡片从 INPUTING 直接被 fail_card 拉到 FAILED，
        # 不再触发 _stream_end 的空卡片终态化。
        resp_metadata = getattr(response, "metadata", {}) or {}
        if resp_metadata.get("notification_type") == "system":
            await self._deliver_system_notification(
                response, chat_id, card_id, msg_id, resp_metadata,
            )
            return

        content_reply = str(response.content or "")
        outbound_media = getattr(response, "media", []) or media or []
        resp_metadata = self._build_resp_metadata(card_id, msg_id)

        # Branch E: 非流式
        if not use_streaming:
            await self._deliver_text_response(
                chat_id, content_reply, outbound_media, resp_metadata,
                branch_label="non-streaming",
            )
            return

        # Branch D: 流式但无 card_id
        if not card_id:
            await self._deliver_text_response(
                chat_id, content_reply, outbound_media, resp_metadata,
                branch_label="markdown-fallback",
            )
            return

        # Branch C: 流式 + card 未被流式处理
        if not self.sender.is_card_handled_by_streaming(card_id):
            await self._deliver_text_response(
                chat_id, content_reply, outbound_media, resp_metadata,
                branch_label="unhandled-card",
            )
            return

        # Branch A/B: 流式 + card 已被流式处理，按 exit_reason 分流
        exit_reason = (getattr(response, "metadata", {}) or {}).get("exit_reason")
        if exit_reason == "max_iterations":
            await self._deliver_max_iterations(card_id, chat_id, msg_id, content_reply, outbound_media)
        else:
            await self._deliver_normal_completion(card_id, chat_id, outbound_media)

    async def _deliver_system_notification(
        self, response: Any, chat_id: str, card_id: str | None,
        msg_id: str | None, meta: dict[str, Any],
    ) -> None:
        """投递系统通知（命令响应 / 错误通知），不走 LLM 卡片流式路径。

        - error 场景：卡片 FAILED + 非空文案，不残留空 FINISHED 卡片。
        - info/warning 场景（如 /new /stop）：卡片若有流式内容则终态化，否则 markdown。

        Args:
            response: 系统通知 OutboundMessage（含 content / media / metadata）。
            chat_id: 会话 ID。
            card_id: 已创建的 AI Card 实例 ID（可能为 None）。
            msg_id: 消息唯一 ID（用于卡片终态化与情感表情跟踪）。
            meta: 系统通知 metadata（含 severity 等）。
        """
        severity = meta.get("severity", "info")
        content = str(response.content or "")
        outbound_media = getattr(response, "media", []) or []
        resp_metadata = self._build_resp_metadata(card_id, msg_id)

        if card_id and severity == "error":
            # 错误完结的唯一出口：fail_card（用流式 isError 语义终态化）。
            # 若卡片已通过流式推过部分内容（工具执行出错等中途失败），
            # 保留半截进度，追加失败提示；否则仅展示失败文案。
            # 错误不再经 on_stream_end 流式链路表达，避免双路径二次终态化。
            if self.card_manager:
                error_text = content
                if self.sender is not None:
                    partial = self.sender.take_stream_buffer(card_id)
                    if partial.strip():
                        error_text = f"{partial.strip()}\n\n---\n⚠️ {content}"
                logger.debug(
                    "'[CARD-DEBUG] error notification card={} error_text={!r}'",
                    card_id, error_text[:200],
                )
                delivered = await self.card_manager.fail_card(card_id, error_text)
                if not delivered:
                    # fail_card 两步均失败：卡片无法渲染，回落 markdown 兜底，
                    # 避免卡片永久停在 INPUTING 且用户看不到任何错误提示。
                    await self._deliver_text_response(
                        chat_id, content, outbound_media, resp_metadata,
                        branch_label="system-error-fallback",
                    )
            else:
                await self._deliver_text_response(
                    chat_id, content, outbound_media, resp_metadata,
                    branch_label="system-error-fallback",
                )
            return

        if card_id:
            # info/warning 通知：卡片若有流式内容则终态化，否则 markdown 兜底
            if self.sender.is_card_handled_by_streaming(card_id):
                await self.sender.finalize_card_with_notification(
                    card_id, msg_id or chat_id, content,
                )
            else:
                await self._deliver_text_response(
                    chat_id, content, outbound_media, resp_metadata,
                    branch_label="system-notification-markdown",
                )
            return

        # 无卡片：直接 markdown
        await self._deliver_text_response(
            chat_id, content, outbound_media, resp_metadata,
            branch_label="system-notification-markdown",
        )

    async def _deliver_text_response(
        self, chat_id: str, content_reply: str, outbound_media: list[str],
        resp_metadata: dict[str, str], branch_label: str,
    ) -> None:
        """统一投递文本+媒体响应到 sender（Branch C/D/E 共享）。

        Args:
            branch_label: 日志中标识分支的标签，如 ``"non-streaming"`` /
                ``"markdown-fallback"`` / ``"unhandled-card"``。
        """
        if not content_reply and not outbound_media:
            return
        self.logger.debug(
            "[{}] chat_id={}, content_len={}, media={}",
            branch_label, chat_id, len(content_reply), outbound_media,
        )
        await self.sender.send(SimpleNamespace(
            channel=self.name, chat_id=chat_id,
            content=content_reply,
            metadata=resp_metadata, media=outbound_media,
        ))

    async def _deliver_normal_completion(
        self, card_id: str, chat_id: str, outbound_media: list[str],
    ) -> None:
        """Branch B: 流式正常完成，内容已通过 card 送达，仅投递 media。"""
        self.logger.debug(
            "[STREAM] Card {} handled by streaming, skipping duplicate send "
            "(chat={})", card_id, chat_id)
        if outbound_media:
            await self._send_media(card_id, chat_id, outbound_media)

    async def _deliver_max_iterations(
        self, card_id: str, chat_id: str, msg_id: str | None,
        content_reply: str, outbound_media: list[str],
    ) -> None:
        """Branch A: 流式被 max_iterations 打断，在卡片上追加通知并投递 media。"""
        self.logger.debug(
            "[STREAM] Card {} truncated by max_iterations, appending notification "
            "(chat={})", card_id, chat_id)
        await self.sender.finalize_card_with_notification(
            card_id, msg_id or chat_id, content_reply,
        )
        if outbound_media:
            await self._send_media(card_id, chat_id, outbound_media)

    async def _send_media(
        self, card_id: str, chat_id: str, outbound_media: list[str],
    ) -> None:
        """投递媒体附件到指定 card。"""
        await self.sender.send(SimpleNamespace(
            channel=self.name, chat_id=chat_id,
            content="", metadata={"_card_id": card_id},
            media=outbound_media,
        ))

    async def _fail_pending_card(self, card_id: str | None = None) -> None:
        """出错时清理挂起的 AI Card，避免永久 INPUTING。

        card_id 由 message.py 直接传入，无需共享 dict。
        """
        if card_id and self.card_manager:
            try:
                await self.card_manager.fail_card(card_id, "Internal processing error")
            except Exception:
                pass

    def _make_progress_callback(self, chat_id: str, *, msg_id: str | None = None) -> Any:
        """创建进度回调：当工具开始执行时更新 DingTalk emotion 显示工具名。

        返回的闭包签名与 ``on_progress`` 兼容：
        async (delta: str, *, tool_hint: bool = False, tool_events=None) -> None
        """
        async def _on_progress(
            delta: str, *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            if tool_hint and self.sender and tool_events:
                for event in tool_events:
                    tool_name = event.get("name", "")
                    if tool_name:
                        # 显示工具名称细节（如 "🔧list_dir"），
                        # 不走标准 state 查找，直接作为原始 emotion 名称
                        lookup_id = msg_id or chat_id
                        await self.sender._trigger_emotion(
                            lookup_id, f"🔧{tool_name}",
                        )
                        break
        return _on_progress

    def _make_stream_callback(
        self, chat_id: str, *, card_id: str | None = None, msg_id: str | None = None,
    ) -> Any:
        """创建流式回调：LLM 每段 text delta → DingTalk AI Card 增量更新。

        card_id 通过闭包嵌入 metadata，sender 直接使用，无需查共享 dict。
        """
        async def _on_stream(delta: str) -> None:
            if self.sender and delta:
                metadata: dict[str, Any] = {"_stream_delta": True}
                if card_id:
                    metadata["_card_id"] = card_id
                if msg_id:
                    metadata["msg_id"] = msg_id
                await self.sender.send(SimpleNamespace(
                    channel=self.name, chat_id=chat_id,
                    content=delta, media=(),
                    metadata=metadata,
                ))
        return _on_stream

    def _make_stream_end_callback(
        self, chat_id: str, *, card_id: str | None = None, msg_id: str | None = None,
    ) -> Any:
        """创建流结束回调：通知 sender 流暂停（工具调用）或流结束。

        card_id 通过闭包嵌入 metadata，sender 直接使用。
        """
        async def _on_stream_end(*, resuming: bool) -> None:
            if self.sender:
                metadata: dict[str, Any] = {"_stream_end": True, "_resuming": resuming}
                if card_id:
                    metadata["_card_id"] = card_id
                if msg_id:
                    metadata["msg_id"] = msg_id
                await self.sender.send(SimpleNamespace(
                    channel=self.name, chat_id=chat_id,
                    content="", media=(),
                    metadata=metadata,
                ))
        return _on_stream_end

    def _get_robot_code(self) -> str:
        robot_code = os.environ.get("DINGTALK_ROBOT_CODE")
        if robot_code:
            return robot_code
        if hasattr(self, "_client") and self._client is not None:
            client_robot = getattr(self._client, "robot_code", None)
            if client_robot:
                return client_robot
        return (self.dingtalk_config.client_id if self.dingtalk_config else "")


__all__ = ["DingTalkChannelPlugin", "DingTalkConfig"]
