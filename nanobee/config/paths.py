"""Config paths utilities (MVP 最小化版本)."""
from __future__ import annotations

from pathlib import Path


def get_media_dir() -> Path:
    """返回媒体文件存储根目录（MVP: 默认 ~/.nanobee/media）。"""
    return Path.home() / ".nanobee" / "media"
