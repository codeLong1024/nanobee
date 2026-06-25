"""DingTalk authentication and SDK imports.

This module handles:
- DingTalk Stream SDK availability check
- Credential management
- SDK imports with fallback for graceful degradation
"""

from __future__ import annotations

try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotHandler, ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback so class definitions don't crash at module level
    # Use a dummy base class instead of None to allow inheritance
    class _DummyBase:  # type: ignore[no-redef]
        pass
    CallbackHandler = _DummyBase  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotHandler = _DummyBase  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]
    Credential = None  # type: ignore[assignment,misc]
    DingTalkStreamClient = None  # type: ignore[assignment,misc]


__all__ = [
    "DINGTALK_AVAILABLE",
    "AckMessage",
    "CallbackMessage",
    "ChatbotHandler",
    "ChatbotMessage",
    "Credential",
    "DingTalkStreamClient",
]
