"""Config paths utilities (MVP 最小化版本).

媒体目录优先级：
1. 环境变量 NANOBEE_DATA_DIR（如 /nanobee-data/<instance> → /nanobee-data/<instance>/media）
2. 回退到 ~/.nanobee/media（向后兼容）
"""
from __future__ import annotations

import os
from pathlib import Path


def get_media_dir() -> Path:
    """返回媒体文件存储根目录。

    优先从 NANOBEE_DATA_DIR 环境变量推导，否则回退到默认路径。
    """
    data_dir = os.environ.get("NANOBEE_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "media"
    return Path.home() / ".nanobee" / "media"
