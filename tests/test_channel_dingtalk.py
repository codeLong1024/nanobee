"""Tests for channel_dingtalk — DingTalk channel for nanobee.

Covers:
- Session key generation (build_session_key, is_group_session, parse_group_session)
- Config validation (DingTalkConfig defaults)
- Emotion state machine (DingTalkEmotionHook: state transitions, dedup, raw names)
- DingTalkSender per-message emotion management (msg_id keying)
- Message parsing basics (ParsedMessage, msg_id propagation)
"""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
from nanobee.builtin.channel_dingtalk.emotion_hook import (
    DingTalkEmotionHook,
    EmotionContext,
)
from nanobee.builtin.channel_dingtalk.message import ParsedMessage
from nanobee.builtin.channel_dingtalk.session import (
    build_session_key,
    is_group_session,
    parse_group_session,
)


# ============================================================
# Session
# ============================================================


class TestSession:
    def test_build_private(self):
        assert build_session_key("user123") == "user123"

    def test_build_private_with_conv_type_1(self):
        assert build_session_key("user123", "1", "conv456") == "user123"

    def test_build_group(self):
        key = build_session_key("user123", "2", "conv456")
        assert key == "dingtalk:group:user123@conv456"

    def test_build_group_no_conversation_id(self):
        # conversation_type == "2" but no conversation_id → treat as private
        assert build_session_key("user123", "2") == "user123"

    def test_is_group_session_true(self):
        assert is_group_session("dingtalk:group:user@conv") is True

    def test_is_group_session_false(self):
        assert is_group_session("user123") is False
        assert is_group_session("") is False

    def test_parse_group_valid(self):
        sender, conv = parse_group_session("dingtalk:group:alice@room42")
        assert sender == "alice"
        assert conv == "room42"

    def test_parse_group_private(self):
        assert parse_group_session("user123") == (None, None)

    def test_parse_group_malformed(self):
        assert parse_group_session("dingtalk:group:nosep") == (None, None)


# ============================================================
# Config
# ============================================================


class TestDingTalkConfig:
    def test_defaults(self):
        cfg = DingTalkConfig()
        assert cfg.enabled is False
        assert cfg.client_id == ""
        assert cfg.client_secret == ""
        assert cfg.streaming is True
        assert cfg.allow_from == []
        assert cfg.enable_media_upload is True
        assert cfg.media_max_mb == 20
        assert cfg.enable_marker_processing is True
        assert cfg.stream_buffer_max_chars == 500_000
        assert cfg.markdown_title == "智能体回复"

    def test_custom_values(self):
        cfg = DingTalkConfig(client_id="test-id", client_secret="test-secret",
                             streaming=False, enabled=True)
        assert cfg.client_id == "test-id"
        assert cfg.client_secret == "test-secret"
        assert cfg.streaming is False
        assert cfg.enabled is True


# ============================================================
# Emotion hook — state machine
# ============================================================


@pytest.fixture
def mock_http() -> MagicMock:
    http = MagicMock()
    http.post = AsyncMock()
    http.post.return_value.status_code = 200
    return http


@pytest.fixture
def emotion_ctx(mock_http) -> EmotionContext:
    return EmotionContext(
        http_client=mock_http,
        token="test-token",
        robot_code="test-robot",
        open_msg_id="msg-001",
        open_conversation_id="conv-001",
    )


class TestDingTalkEmotionHook:
    """Test the emotion state machine (DingTalkEmotionHook.update)."""

    @pytest.mark.asyncio
    async def test_update_thinking(self, emotion_ctx: EmotionContext):
        """transition to 'thinking' calls update_emotion with correct name."""
        hook = DingTalkEmotionHook(emotion_ctx)
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await hook.update("thinking")
            mock_update.assert_awaited_once_with(
                http_client=emotion_ctx.http_client,
                token="test-token",
                robot_code="test-robot",
                open_msg_id="msg-001",
                open_conversation_id="conv-001",
                emotion_name="🤔思考中",
            )

    @pytest.mark.asyncio
    async def test_update_writing(self, emotion_ctx: EmotionContext):
        hook = DingTalkEmotionHook(emotion_ctx)
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await hook.update("writing")
            mock_update.assert_awaited_once()
            _, kwargs = mock_update.await_args
            assert kwargs["emotion_name"] == "✍️输出中"

    @pytest.mark.asyncio
    async def test_update_duplicate_skip(self, emotion_ctx: EmotionContext):
        """same state twice → second call is skipped (no API call)."""
        hook = DingTalkEmotionHook(emotion_ctx)
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await hook.update("writing")
            assert mock_update.await_count == 1
            await hook.update("writing")  # duplicate
            assert mock_update.await_count == 1  # no extra call

    @pytest.mark.asyncio
    async def test_update_state_tracking(self, emotion_ctx: EmotionContext):
        """internal current_emotion tracks the latest state."""
        hook = DingTalkEmotionHook(emotion_ctx)
        assert emotion_ctx.current_emotion is None
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()):
            await hook.update("thinking")
            assert emotion_ctx.current_emotion == "thinking"
            await hook.update("writing")
            assert emotion_ctx.current_emotion == "writing"

    @pytest.mark.asyncio
    async def test_update_raw_name_passthrough(self, emotion_ctx: EmotionContext):
        """unknown state name is passed directly as emotion display text."""
        hook = DingTalkEmotionHook(emotion_ctx)
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await hook.update("🔧list_dir")
            mock_update.assert_awaited_once()
            _, kwargs = mock_update.await_args
            assert kwargs["emotion_name"] == "🔧list_dir"


# ============================================================
# DingTalkSender — msg_id emotion keying
# ============================================================


@pytest.fixture
def sender_config() -> DingTalkConfig:
    return DingTalkConfig(client_id="test-id", client_secret="test-secret")


@pytest.fixture
def mock_card_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.stream_content = AsyncMock()
    mgr.finish_streaming = AsyncMock()
    mgr.finish_card_status = AsyncMock()
    mgr.finalize_card = AsyncMock()
    mgr.finalize_card.return_value = True
    return mgr


@pytest.fixture
def sender(sender_config, mock_card_manager) -> MagicMock:
    """Create a DingTalkSender with mocks, with internal routing methods patched.

    We mock _send_batch_message to avoid HTTP, set a fake token, and
    patch emotion_handler so _trigger_emotion works without network.
    """
    from nanobee.builtin.channel_dingtalk.sender import DingTalkSender

    http = MagicMock()
    http.post = AsyncMock()
    http.post.return_value.status_code = 200

    s = DingTalkSender.__new__(DingTalkSender)
    s.config = sender_config
    s.logger = MagicMock()
    s._http = http
    s._card_manager = mock_card_manager
    s._streaming_buffers = {}
    s._overflow_cards = set()
    s._streamed_cards: OrderedDict[str, bool] = OrderedDict()
    s._card_has_streamed = set()
    s._emotion_contexts = {}

    # Mock token manager to return a fake token
    s._token_manager = MagicMock()
    s._token_manager.get_access_token = AsyncMock(return_value="fake-token")

    # Mock _send_batch_message to avoid HTTP
    s._send_batch_message = AsyncMock(return_value=True)

    # Patch update_emotion at the emotion_handler level so
    # DingTalkEmotionHook.update() does not actually call HTTP
    patcher = patch(
        "nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
        new=AsyncMock(),
    )
    patcher.start()

    yield s

    patcher.stop()


@pytest.fixture
def emotion_ctx_for_sender(sender) -> EmotionContext:
    """Pre-built EmotionContext stored in sender under a known msg_id."""
    ctx = EmotionContext(
        http_client=MagicMock(),
        token="fake-token",
        robot_code="test-robot",
        open_msg_id="msg-001",
        open_conversation_id="conv-001",
    )
    sender._emotion_contexts["msg-001"] = ctx
    return ctx


def _make_msg(
    content: str = "",
    chat_id: str = "conv-test",
    metadata: dict | None = None,
    media: list[str] | None = None,
) -> SimpleNamespace:
    """Helper: create a SimpleNamespace that mimics what channel.py sends."""
    return SimpleNamespace(
        channel="channel_dingtalk",
        chat_id=chat_id,
        content=content,
        metadata=metadata or {},
        media=media or [],
    )


class TestDingTalkSenderEmotionKey:
    """Verify that all emotion operations are keyed by msg_id, not chat_id."""

    @pytest.mark.asyncio
    async def test_trigger_emotion_finds_ctx_by_msg_id(self, sender, emotion_ctx_for_sender):
        """_trigger_emotion(msg_id) looks up the EmotionContext by msg_id."""
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender._trigger_emotion("msg-001", "writing")
            mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trigger_emotion_missing_msg_id_returns_silently(
        self, sender, emotion_ctx_for_sender,
    ):
        """_trigger_emotion with unknown msg_id silently returns (no crash)."""
        await sender._trigger_emotion("msg-999", "writing")  # must not raise

    @pytest.mark.asyncio
    async def test_recall_emotion_by_msg_id(self, sender, emotion_ctx_for_sender):
        """_recall_emotion(msg_id) looks up ctx by msg_id and cleans up."""
        with patch(
            "nanobee.builtin.channel_dingtalk.sender._recall_thinking_emoji",
            new=AsyncMock(),
        ) as mock_recall:
            await sender._recall_emotion("msg-001")
            mock_recall.assert_awaited_once()
        # ctx should be cleaned up after recall
        assert "msg-001" not in sender._emotion_contexts

    @pytest.mark.asyncio
    async def test_recall_emotion_missing_msg_id_returns_silently(
        self, sender, emotion_ctx_for_sender,
    ):
        """_recall_emotion with unknown msg_id silently returns."""
        await sender._recall_emotion("msg-999")  # must not raise

    def test_cleanup_chat_context_by_msg_id(self, sender, emotion_ctx_for_sender):
        """_cleanup_chat_context(msg_id) removes the emotion context."""
        assert "msg-001" in sender._emotion_contexts
        sender._cleanup_chat_context("msg-001")
        assert "msg-001" not in sender._emotion_contexts

    def test_cleanup_chat_context_missing_msg_id(self, sender):
        """_cleanup_chat_context with unknown msg_id silently returns."""
        sender._cleanup_chat_context("nonexistent")  # must not raise


class TestDingTalkSenderSendEmotion:
    """Verify that send() uses msg_id from metadata for emotion operations."""

    @pytest.mark.asyncio
    async def test_send_streaming_start_uses_msg_id(self, sender):
        """send() with _stream_delta starts emotion lookup by msg_id."""
        msg = _make_msg(
            content="hello",
            chat_id="conv-test",
            metadata={"_stream_delta": True, "_card_id": "card-001", "msg_id": "msg-001"},
        )
        sender._emotion_contexts["msg-001"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-001", open_conversation_id="conv-001",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender.send(msg)
            mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_streaming_end_uses_msg_id(self, sender):
        """send() with _stream_end calls _trigger_emotion(msg_id, 'done')."""
        msg = _make_msg(
            content="final content",
            chat_id="conv-test",
            metadata={"_stream_end": True, "_card_id": "card-001", "_resuming": False,
                      "msg_id": "msg-002"},
        )
        sender._emotion_contexts["msg-002"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-002", open_conversation_id="conv-002",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender.send(msg)
            assert mock_update.await_count >= 1

    @pytest.mark.asyncio
    async def test_send_streaming_end_resume_uses_msg_id(self, sender):
        """send() with _stream_end + _resuming uses msg_id for 'tool' emotion."""
        msg = _make_msg(
            content="",
            chat_id="conv-test",
            metadata={"_stream_end": True, "_card_id": "card-002", "_resuming": True,
                      "msg_id": "msg-003"},
        )
        sender._emotion_contexts["msg-003"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-003", open_conversation_id="conv-003",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender.send(msg)
            assert mock_update.await_count >= 1

    @pytest.mark.asyncio
    async def test_send_non_streaming_card_uses_msg_id(self, sender):
        """Non-streaming path with card_id uses msg_id for 'done' emotion + cleanup."""
        msg = _make_msg(
            content="card content",
            chat_id="conv-test",
            metadata={"_card_id": "card-003", "msg_id": "msg-004"},
        )
        sender._emotion_contexts["msg-004"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-004", open_conversation_id="conv-004",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()):
            await sender.send(msg)
        assert "msg-004" not in sender._emotion_contexts

    @pytest.mark.asyncio
    async def test_send_pure_media_cleanup(self, sender):
        """Pure media path (card_id + media, no content) cleans up by msg_id."""
        msg = _make_msg(
            content="",
            chat_id="conv-test",
            metadata={"_card_id": "card-004", "msg_id": "msg-005"},
            media=["https://example.com/img.png"],
        )
        sender._emotion_contexts["msg-005"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-005", open_conversation_id="conv-005",
        )
        await sender.send(msg)
        assert "msg-005" not in sender._emotion_contexts

    @pytest.mark.asyncio
    async def test_send_fallback_markdown_recall_emotion(self, sender):
        """Markdown fallback path uses msg_id for _recall_emotion."""
        msg = _make_msg(
            content="fallback text",
            chat_id="conv-test",
            metadata={"msg_id": "msg-006"},
        )
        sender._emotion_contexts["msg-006"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-006", open_conversation_id="conv-006",
        )
        with patch(
            "nanobee.builtin.channel_dingtalk.sender._recall_thinking_emoji",
            new=AsyncMock(),
        ) as mock_recall:
            await sender.send(msg)
            assert mock_recall.await_count >= 1
        assert "msg-006" not in sender._emotion_contexts

    @pytest.mark.asyncio
    async def test_send_fallback_to_chat_id_when_no_msg_id(self, sender):
        """When metadata has no msg_id, send() falls back to chat_id for emotion key."""
        msg = _make_msg(
            content="no msg_id in metadata",
            chat_id="conv-chatfallback",
            metadata={},
        )
        sender._emotion_contexts["conv-chatfallback"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-fallback", open_conversation_id="conv-fallback",
        )
        with patch(
            "nanobee.builtin.channel_dingtalk.sender._recall_thinking_emoji",
            new=AsyncMock(),
        ) as mock_recall:
            await sender.send(msg)
            assert mock_recall.await_count >= 1
        assert "conv-chatfallback" not in sender._emotion_contexts

    @pytest.mark.asyncio
    async def test_send_progress_skip_does_not_trigger_emotion(self, sender):
        """_progress messages are skipped and do NOT trigger emotion operations."""
        msg = _make_msg(
            content="progress",
            chat_id="conv-test",
            metadata={"_progress": True, "_card_id": "card-005", "msg_id": "msg-007"},
        )
        sender._emotion_contexts["msg-007"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-007", open_conversation_id="conv-007",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender.send(msg)
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_overflow_card_with_msg_id(self, sender):
        """Overflow path still uses msg_id when triggering emotion."""
        sender.config.stream_buffer_max_chars = 10
        msg = _make_msg(
            content="A" * 20,
            chat_id="conv-test",
            metadata={"_stream_delta": True, "_card_id": "card-overflow",
                      "msg_id": "msg-overflow"},
        )
        sender._emotion_contexts["msg-overflow"] = EmotionContext(
            http_client=MagicMock(), token="t", robot_code="r",
            open_msg_id="msg-overflow", open_conversation_id="conv-overflow",
        )
        with patch("nanobee.builtin.channel_dingtalk.emotion_hook.update_emotion",
                   new=AsyncMock()) as mock_update:
            await sender.send(msg)
            mock_update.assert_awaited_once()


# ============================================================
# Message parsing
# ============================================================


class TestParsedMessage:
    def test_minimal_fields(self):
        """ParsedMessage requires msg_id and properly stores it."""
        msg = ParsedMessage(
            content="test",
            sender_id="user001",
            sender_name="Alice",
            conversation_type="2",
            conversation_id="conv001",
            chat_id="conv001",
            session_key="dingtalk:group:user001@conv001",
            msg_id="msg-abc-123",
            raw_message=MagicMock(),
        )
        assert msg.msg_id == "msg-abc-123"
        assert msg.content == "test"
        assert msg.sender_id == "user001"

    def test_private_chat_chat_id(self):
        """Private chat uses sender_id as chat_id."""
        msg = ParsedMessage(
            content="hi",
            sender_id="user001",
            sender_name="Alice",
            conversation_type="1",
            conversation_id="",
            chat_id="user001",
            session_key="user001",
            msg_id="msg-xyz-789",
            raw_message=MagicMock(),
        )
        assert msg.chat_id == "user001"
        assert msg.msg_id == "msg-xyz-789"

    def test_group_chat_chat_id(self):
        """Group chat uses conversation_id as chat_id."""
        msg = ParsedMessage(
            content="hello all",
            sender_id="user001",
            sender_name="Alice",
            conversation_type="2",
            conversation_id="group-conv-001",
            chat_id="group-conv-001",
            session_key="dingtalk:group:user001@group-conv-001",
            msg_id="msg-group-001",
            raw_message=MagicMock(),
        )
        assert msg.chat_id == "group-conv-001"
        assert msg.msg_id == "msg-group-001"

    def test_default_media_empty_list(self):
        """ParsedMessage.media defaults to empty list."""
        msg = ParsedMessage(
            content="test",
            sender_id="u1",
            sender_name="U1",
            conversation_type="1",
            conversation_id="",
            chat_id="u1",
            session_key="u1",
            msg_id="m1",
            raw_message=MagicMock(),
        )
        assert msg.media == []

    def test_default_sender_staff_id_none(self):
        """ParsedMessage.sender_staff_id defaults to None."""
        msg = ParsedMessage(
            content="test",
            sender_id="u1",
            sender_name="U1",
            conversation_type="1",
            conversation_id="",
            chat_id="u1",
            session_key="u1",
            msg_id="m1",
            raw_message=MagicMock(),
        )
        assert msg.sender_staff_id is None


# ============================================================
# EmotionContext dataclass
# ============================================================


class TestEmotionContext:
    def test_create(self):
        http = MagicMock()
        ctx = EmotionContext(
            http_client=http,
            token="tok",
            robot_code="bot",
            open_msg_id="msg-1",
            open_conversation_id="conv-1",
        )
        assert ctx.token == "tok"
        assert ctx.robot_code == "bot"
        assert ctx.open_msg_id == "msg-1"
        assert ctx.current_emotion is None  # init=False field

    def test_current_emotion_tracking(self):
        ctx = EmotionContext(
            http_client=MagicMock(),
            token="tok",
            robot_code="bot",
            open_msg_id="m1",
            open_conversation_id="c1",
        )
        ctx.current_emotion = "thinking"
        assert ctx.current_emotion == "thinking"
