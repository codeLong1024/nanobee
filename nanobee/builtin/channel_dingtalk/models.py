"""Data models for DingTalk AI Card operations."""

from __future__ import annotations


class AICardStatus:
    """DingTalk AI Card status codes."""
    INPUTING = "2"
    FINISHED = "3"
    # 注意：不定义 FAILED("5")。官方 SDK 语义下 FAILED 态仅携带 msgTitle/logo，
    # 客户端会清空内容（卡片"一闪而过"），失败场景统一用 FINISHED + 错误文案（fail_card）。


__all__ = ["AICardStatus"]
