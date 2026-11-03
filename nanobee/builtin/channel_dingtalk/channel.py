"""DingTalk Channel main class for nanobee.

Uses Stream SDK (WebSocket) for message receiving.
Uses HTTP API (via DingTalkSender) for sending messages.
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from nanobee.channel.base import ChannelPlugin
from nanobee.outbound import OutboundMessage as BeeOutboundMessage
from .auth import (
    DINGTALK_AVAILABLE,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)
from .card_client import DingTalkCardClient
from .card_manager import CardManager
from .card_pipeline import CardHandle, CardPipeline, FinishMode, LegacyCardPipeline
from .config import DingTalkConfig
from .media.download import get_download_root
from .message import NanobeeDingTalkHandler
from .rate_limiter import RateLimiter
from .sender import DingTalkSender
from .v2_card_session import V2CardPipeline

from nanobee.utils.logger import logger



class DingTalkChannelPlugin(ChannelPlugin):
    """DingTalk channel using Stream Mode (SDK) + AI Card streaming for nanobee."""

    name = "channel_dingtalk"
    display_name = "钉钉机器人"

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self.logger = logger
        self.dingtalk_config: DingTalkConfig | None = None
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None
        self._running = False

        # Rate limiter
        self.rate_limiter = RateLimiter(max_qps=20)

        # 卡片交付实现（legacy / v2；start() 按配置构造一次，运行期零模式判断）
        self.card_pipeline: CardPipeline | None = None
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

    def _resolve_media_local_roots(self, config: DingTalkConfig) -> list[str]:
        """解析本地附件白名单根：``data_dir`` + ``media_local_roots`` + 入站附件目录。

        白名单根必须是**静态根**：媒体读取发生在 turn 之后的事件投递路径上
        （cron/kernel 注入/子代理通知），此时 per-request 的 ``context_root``
        ContextVar 早已复位为 None。``data_dir`` 覆盖 ``users/<user>/workspace/**``
        实测路径，``media_local_roots`` 供额外放行；相对路径按 ``data_dir`` 解析。

        已知取舍：``data_dir`` 意味着同实例内其他用户目录也可读（用户已拍板，
        单实例多为单用户）；若要跨用户隔离，应改为只放行当前用户的 workspace。
        入站附件目录（``./media/dingtalk``，用户上传的文件）必须放行，否则用户
        自己的附件在回投时会被安全策略拒绝。
        """
        data_dir = getattr(self.kernel, "data_dir", None)
        base = Path(data_dir).expanduser() if isinstance(data_dir, (str, Path)) and str(data_dir).strip() else None

        roots: list[str] = []
        candidates: list[Path] = []
        if base is not None:
            candidates.append(base)
        for raw in config.media_local_roots or ():
            text = str(raw).strip()
            if not text or text in (".", ".."):
                continue
            candidate = Path(text).expanduser()
            if not candidate.is_absolute() and base is not None:
                candidate = base / candidate
            candidates.append(candidate)
        candidates.append(get_download_root())
        for candidate in candidates:
            resolved = str(candidate.resolve(strict=False))
            if resolved not in roots:
                roots.append(resolved)
        return roots

    def _apply_media_policy(self, sender: DingTalkSender) -> None:
        """把 ``DingTalkConfig`` 的媒体配置注入 sender（唯一收敛点的策略来源）。"""
        config = self.dingtalk_config
        if config is None:
            return
        max_mb = int(config.media_max_mb)
        roots = self._resolve_media_local_roots(config)
        sender.set_media_policy(
            max_bytes=max_mb * 1024 * 1024,
            local_roots=roots,
            allow_media=bool(config.enable_media_upload),
        )
        # 预格式化单参数：loguru 无 %-占位，避免与 stdlib logging 规则误判
        self.logger.info(
            f"媒体读取策略: enabled={config.enable_media_upload} "
            f"max_mb={max_mb} local_roots={roots}"
        )

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
        self._apply_media_policy(self.sender)

        # 创建 card_client + card_manager（需要 sender 的 token 函数）
        self.card_client = DingTalkCardClient(
            access_token_fn=self.sender.get_access_token,
            proxy_url=self.dingtalk_config.proxy_url,
        )
        self.card_manager = CardManager(self.card_client)

        # 卡片交付实现：构造期一次选择（运行期不再有任何模式判断）
        if self.dingtalk_config.card_render_mode == "v2":
            self.card_pipeline = V2CardPipeline(
                self.card_client,
                push_interval=self.dingtalk_config.stream_push_min_interval,
                template_id=self.dingtalk_config.card_template_id,
                agent_name=self.metadata.name,
                model="",  # 模型名由内核注入进度事件前不可得，留空省略展示
                show_metadata=self.dingtalk_config.card_show_metadata,
                rate_limiter=self.rate_limiter,
            )
            self.logger.info("[V2CARD] 结构化进度卡模式已启用")
        else:
            self.card_pipeline = LegacyCardPipeline(
                sender=self.sender,
                card_manager=self.card_manager,
                channel_name=self.name,
                logger=self.logger,
            )

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
        """覆盖基类：为子代理/cron 等内部消息创建 AI Card 投递（含附件）。

        子代理完成后，_injector → enqueue_message → _dispatch → agent.outbound 事件
        到达此处。基类 `_on_agent_outbound` 走裸 markdown，本覆盖改为创建 AI Card
        （打字效果 + Card UI）+ 附件投递；失败时回退到基类（基类已透传 media，
        回退路径会经 `send()` → `sender.send()` 把附件一并投出）。

        守卫与基类一致：正文与附件至少一个非空——**纯附件**（正文为空）不尝试
        创建卡片（`content.strip()` 为假天然跳过），直接走回退路径投递附件。
        """
        if not isinstance(data, dict):
            return
        channel_name = data.get("channel", "")
        if channel_name != self.metadata.name:
            return
        chat_id = data.get("chat_id", "direct")
        # ``or ""`` 归一 None：旧发布方可能不带类型归一，而下方 content.strip() 不容忍 None
        content = data.get("content") or ""
        media = data.get("media") or []
        if not content and not media:
            return

        # 尝试通过 AI Card 投递（含附件）
        if self.sender and content.strip():
            token = await self.sender.get_access_token()
            if token:
                try:
                    ok = await self.sender.send_via_card(
                        token, chat_id, content.strip(), media=media,
                    )
                    if ok:
                        return  # Card 投递成功，不发送 markdown
                except Exception:
                    self.logger.exception(
                        "_on_agent_outbound: send_via_card failed for chat={}", chat_id,
                    )

        # Card 不可用或失败，回退到基类（media 由基类透传给 send()）
        await super()._on_agent_outbound(data)

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
        handle: CardHandle | None = None,
        msg_id: str | None = None,
    ) -> None:
        """处理入站消息 — 路由到 nanobee 内核并投递响应。

        消息处理分为两步：
        1. 调用内核 handle_message（流式/非流式两种模式）
        2. 将响应经卡片交付实现投递（终态 / markdown 兜底 + media）

        ``handle`` 是本次 turn 的卡片所有权句柄（由 ``_handle_message`` 开卡
        得到），随回调闭包流动——不使用任何全局键查找，因此并发消息之间不会
        互相覆盖卡片。
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
                    on_stream=self._make_stream_callback(handle),
                    on_stream_end=self._make_stream_end_callback(handle),
                    on_progress=self._make_progress_callback(handle),
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
                    on_progress=self._make_progress_callback(handle),
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
            await self._deliver_response(response, handle, chat_id, use_streaming, media)
        except Exception:
            self.logger.exception("Error processing DingTalk message")
            await self._fail_pending_card(handle)

    # ------------------------------------------------------------------
    # 响应投递 — 5 种分支场景
    # ------------------------------------------------------------------

    async def _deliver_response(
        self,
        response: Any,
        handle: CardHandle | None,
        chat_id: str,
        use_streaming: bool,
        media: list[str] | None,
    ) -> None:
        """投递内核响应 —— 出口分支语义的唯一判定点。

        分支语义（legacy 与 v2 完全一致，各实现只负责映射到自己的卡片原语）：
        S) 系统通知：error → 失败终态；info/warning → 终态追加通知
        E) 非流式 → markdown
        D) 流式但无卡片 → markdown
        C) 卡片未经流式处理（终态化会得到空卡）→ markdown
        A) max_iterations → 终态追加通知 + media
        B) 正常完成 → 终态（正文已由流式送达）+ media

        卡片侧返回 False 时统一回落 markdown，避免内容丢失。
        """
        if not response or not self.sender:
            return
        resp_meta = getattr(response, "metadata", {}) or {}
        content = str(response.content or "")
        outbound_media = getattr(response, "media", []) or media or []
        # 兜底 metadata 由交付实现声明（卡片协议键如 legacy 的 _card_id 不跨实现传递）
        resp_metadata = handle.pipeline.fallback_metadata(handle) if handle else {}

        # S) 系统通知（命令响应 / 错误通知）：不走 LLM 卡片流式路径
        if resp_meta.get("notification_type") == "system":
            delivered = False
            if handle is not None:
                if resp_meta.get("severity", "info") == "error":
                    delivered = await handle.pipeline.fail(handle, content)
                else:
                    delivered = await handle.pipeline.finish(
                        handle, content=content, mode="append",
                    )
            if delivered:
                return
            await self._deliver_text_response(
                chat_id, content, outbound_media, resp_metadata,
                branch_label="system-notification-markdown",
            )
            return

        # E/D) 非流式或没有卡片可用 → markdown 直发
        if not use_streaming or handle is None:
            await self._deliver_text_response(
                chat_id, content, outbound_media, resp_metadata,
                branch_label="non-streaming" if not use_streaming else "markdown-fallback",
            )
            return

        # C) 卡片未经流式处理：终态化只会得到空卡，回落 markdown
        if not handle.pipeline.is_streaming(handle):
            await self._deliver_text_response(
                chat_id, content, outbound_media, resp_metadata,
                branch_label="unhandled-card",
            )
            return

        # A/B) 卡片终态：max_iterations 追加通知，其余为正常完成
        exit_reason = resp_meta.get("exit_reason")
        finish_mode: FinishMode = "append" if exit_reason == "max_iterations" else "complete"
        delivered = await handle.pipeline.finish(
            handle, content=content, mode=finish_mode,
        )
        if not delivered:
            await self._deliver_text_response(
                chat_id, content, outbound_media, resp_metadata,
                branch_label="terminal-fallback",
            )
            return
        await self._send_media(handle, outbound_media)

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

    async def _send_media(
        self, handle: CardHandle, outbound_media: list[str],
    ) -> None:
        """投递媒体附件（复用既有 sender 出站通道，正文为空仅带附件）。"""
        if not outbound_media:
            return
        await self.sender.send(SimpleNamespace(
            channel=self.name, chat_id=handle.chat_id,
            content="", metadata=handle.pipeline.fallback_metadata(handle),
            media=outbound_media,
        ))

    async def _fail_pending_card(self, handle: CardHandle | None) -> None:
        """出错时清理挂起的卡片，避免卡片永久停在「生成中」。

        统一走交付实现的 ``fail``（协议保证错误文案在卡片上可见）。
        """
        if handle is None:
            return
        try:
            await handle.pipeline.fail(handle, "Internal processing error")
        except Exception:
            self.logger.exception("[CARD] 失败终态推送异常 card={}", handle.card_id)

    def _make_progress_callback(self, handle: CardHandle | None) -> Any:
        """创建进度回调：把工具进度事件转交卡片交付实现。

        返回的闭包签名与 ``on_progress`` 兼容：
        async (delta: str, *, tool_hint: bool = False, tool_events=None) -> None
        """
        async def _on_progress(
            delta: str, *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            if handle is None or not tool_events:
                return
            await handle.pipeline.progress(handle, tool_events)
        return _on_progress

    def _make_stream_callback(self, handle: CardHandle | None) -> Any:
        """创建流式回调：LLM 每段 text delta → 卡片交付实现。

        handle 随闭包携带，交付实现直接使用——无需任何全局键查找。
        """
        async def _on_stream(delta: str) -> None:
            if handle is None or not delta:
                return
            await handle.pipeline.stream_delta(handle, delta)
        return _on_stream

    def _make_stream_end_callback(self, handle: CardHandle | None) -> Any:
        """创建流结束回调：通知交付实现流暂停（工具调用）或流结束。"""
        async def _on_stream_end(*, resuming: bool) -> None:
            if handle is None:
                return
            await handle.pipeline.stream_end(handle, resuming=resuming)
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
