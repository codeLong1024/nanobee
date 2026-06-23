"""Data models for DingTalk AI Card operations."""

from __future__ import annotations


class AICardStatus:
    """DingTalk AI Card status codes."""
    PROCESSING = "1"
    INPUTING = "2"
    FINISHED = "3"
    EXECUTING = "4"
    FAILED = "5"


__all__ = ["AICardStatus"]
