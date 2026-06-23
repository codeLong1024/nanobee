"""Session key generation strategy for DingTalk channel.

Handles:
- Private chat session keys (sender_id)
- Group chat session keys (dingtalk:group:{sender_id}@{conversation_id})
- Session isolation logic for multi-user group chats
"""

from __future__ import annotations

SESSION_KEY_GROUP_PREFIX = "dingtalk:group:"


def build_session_key(
    sender_id: str,
    conversation_type: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Build session key for DingTalk messages.

    Args:
        sender_id: DingTalk user ID (staffId or unionId)
        conversation_type: "2" for group chat, other for private
        conversation_id: Open conversation ID for group chats

    Returns:
        Session key string:
        - Private chat: sender_id
        - Group chat: dingtalk:group:{sender_id}@{conversation_id}
    """
    is_group = conversation_type == "2" and conversation_id
    if is_group:
        # Group chat: each user has independent session history
        return f"{SESSION_KEY_GROUP_PREFIX}{sender_id}@{conversation_id}"
    else:
        # Private chat: use sender_id directly
        return sender_id


def is_group_session(chat_id: str) -> bool:
    """Check if a chat_id represents a group session.

    Args:
        chat_id: The chat/session ID to check

    Returns:
        True if it's a group session (starts with "dingtalk:group:")
    """
    return chat_id.startswith(SESSION_KEY_GROUP_PREFIX)


def parse_group_session(chat_id: str) -> tuple[str | None, str | None]:
    """Parse group session ID to extract sender_id and conversation_id.

    Args:
        chat_id: Session key in format "dingtalk:group:{sender_id}@{conversation_id}"

    Returns:
        Tuple of (sender_id, conversation_id) or (None, None) if invalid format
    """
    if not is_group_session(chat_id):
        return None, None

    try:
        # Format: dingtalk:group:{sender_id}@{conversation_id}
        parts = chat_id[len(SESSION_KEY_GROUP_PREFIX):]  # Remove "dingtalk:group:"
        sender_id, conversation_id = parts.split("@", 1)
        return sender_id, conversation_id
    except (IndexError, ValueError):
        return None, None


__all__ = [
    "SESSION_KEY_GROUP_PREFIX",
    "build_session_key",
    "is_group_session",
    "parse_group_session",
]
