"""DingTalk multi-status emotion state machine.

Drives the message emotion (a sticky DingTalk emoji) through the agent
processing lifecycle by listening to outbound bus stream events:
``_stream_delta`` → ✍️, ``_stream_end`` + ``_resuming`` → 🔧 / ✅.

Usage:
    context = EmotionContext(http, token, robot_code, msg_id, conv_id)
    hook = DingTalkEmotionHook(context)
    await hook.update("writing")   # → ✍️ 输出中
    await hook.update("done")      # → ✅ 已完成

Raw names: any string not in ``_EMOTION_CONFIG`` is used directly as the
emotion display text (e.g. ``"🔧list_dir"``).

Token field in EmotionContext supports both a pre-resolved string and an
async callable (for fresh token per retry attempt).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nanobee.utils.logger import logger

from .emotion_handler import update_emotion

__all__ = [
    "EmotionContext",
    "DingTalkEmotionHook",
]

# State → DingTalk emotion display name (used as ``emotionName`` in the API)
_EMOTION_CONFIG: dict[str, str] = {
    "thinking": "🤔思考中",
    "writing": "✍️输出中",
    "tool": "🔧工具调用中",
    "done": "✅已完成",
}


@dataclass
class EmotionContext:
    """Per-message context required to update the DingTalk emotion via API.

    ``token`` can be a pre-resolved access-token string or an async callable
    that returns a fresh token per invocation (enabling token refresh across
    retry attempts).
    """

    http_client: Any
    token: str | Callable[[], Awaitable[str | None]]
    robot_code: str
    open_msg_id: str
    open_conversation_id: str

    # Internal state tracking — not set by caller
    current_emotion: str | None = field(default=None, init=False, repr=False)


class DingTalkEmotionHook:
    """DingTalk multi-status emotion state machine.

    Maintains the current emotion and avoids redundant API calls for the
    same state.  **Not** a subclass of ``AgentHook`` — the DingTalk channel
    plugin cannot register hooks with the framework's ``AgentLoop`` because
    they live on different sides of the ``MessageBus``.  Instead, this class
    is driven by outbound bus stream events (``_stream_delta``,
    ``_stream_end``) in ``DingTalkSender.send()``.

    On update failure, ``update_emotion`` internally falls back to restoring
    the default thinking emoji.  The ``current_emotion`` tracker is synced
    accordingly: set to ``state_name`` on success, reset to ``"thinking"``
    on fallback.
    """

    def __init__(self, context: EmotionContext) -> None:
        self._ctx = context

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def update(self, state_name: str) -> None:
        """Transition to *state_name* and push the new emotion to DingTalk.

        Args:
            state_name: One of ``"thinking"``, ``"writing"``, ``"tool"``,
                        ``"done"`` (see :data:`_EMOTION_CONFIG`).
                        Any other string is used directly as the emotion
                        display text (e.g. ``"🔧list_dir"``).
        """
        if state_name == self._ctx.current_emotion:
            logger.debug(
                "[Emotion] Skip duplicate state='{}' (msg_id={})",
                state_name, self._ctx.open_msg_id,
            )
            return

        prev = self._ctx.current_emotion

        # 在配置中查找，未找到则视为原始 emotion 名称
        emotion_name = _EMOTION_CONFIG.get(state_name)
        if emotion_name is None:
            emotion_name = state_name  # 原始名称，如 "🔧list_dir"

        logger.debug(
            "[Emotion] {} → {} (msg_id={})",
            prev or "(none)", state_name, self._ctx.open_msg_id,
        )

        ok = await update_emotion(
            http_client=self._ctx.http_client,
            token=self._ctx.token,
            robot_code=self._ctx.robot_code,
            open_msg_id=self._ctx.open_msg_id,
            open_conversation_id=self._ctx.open_conversation_id,
            emotion_name=emotion_name,
        )

        # Sync internal state with the actual result:
        # - Success: commit the new state (matching old behavior)
        # - Failure (fallback restored default): reset to "thinking"
        if ok:
            self._ctx.current_emotion = state_name
        else:
            # Fallback restore of default thinking emoji was used,
            # so the actual current emotion is the thinking state.
            self._ctx.current_emotion = "thinking"
