"""Data models for DingTalk AI Card operations."""

from __future__ import annotations


class AICardStatus:
    """DingTalk AI Card status codes."""
    INPUTING = "2"
    FINISHED = "3"
    FAILED = "5"


__all__ = ["AICardStatus"]
