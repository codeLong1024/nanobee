"""DingTalk rich media constants — ported from services/media/common.ts + utils/constants.ts.

Provides:
- Extension sets for images, audio, video
- Compiled regex patterns for media markers and path detection
"""

from __future__ import annotations

import re

from .helpers import (
    IMAGE_EXTS,
    AUDIO_EXTS,
    VIDEO_EXTS,
    ZIP_BEFORE_UPLOAD_EXTS,
)

# ============ 可读文本文件扩展名 ============
TEXT_FILE_EXTENSIONS: set[str] = {
    ".txt", ".md", ".json", ".csv", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg",
}

# ============ 媒体消息类型（AI Card 豁免） ============
MEDIA_MSG_TYPES: set[str] = {"image", "voice", "file", "video"}

# ============ 正则模式 ============

# 路径匹配字符集：排除空白、反引号、引号、尖括号
# 防止 Markdown 代码块中的路径被吞掉尾部
_PATH_CHARS: str = r"[^\s`\"'<>]+"


def _build_dingtalk_marker_re(tag: str) -> re.Pattern:
    """构建 DingTalk 标记正则：``[DINGTALK_TAG]...[DINGTALK_TAG]``"""
    return re.compile(rf"\[DINGTALK_{tag}\](.*?)\[/DINGTALK_{tag}\]", re.DOTALL)


def _build_raw_path_re(ext_pattern: str) -> re.Pattern:
    """构建裸露媒体路径正则，支持 Unix 和 Windows 绝对路径。"""
    return re.compile(
        rf"(?<![(\[:/])(/ {_PATH_CHARS}\.(?:{ext_pattern})|[A-Za-z]:[/\\] {_PATH_CHARS}\.(?:{ext_pattern}))(?![)\]])",
        re.IGNORECASE,
    )


# 匹配 Markdown 中的本地图片: ![alt](path)
LOCAL_IMAGE_RE: re.Pattern = re.compile(
    r'!\[([^\]]*)\]\(([^)]+)\)'
)

# 匹配裸露的本地图片路径（绝对路径）
BARE_IMAGE_PATH_RE: re.Pattern = _build_raw_path_re("png|jpg|jpeg|gif|webp|bmp|svg")

# 媒体标记
VIDEO_MARKER_RE: re.Pattern = _build_dingtalk_marker_re("VIDEO")
AUDIO_MARKER_RE: re.Pattern = _build_dingtalk_marker_re("AUDIO")
FILE_MARKER_RE: re.Pattern = _build_dingtalk_marker_re("FILE")

# 裸露的媒体路径（用于 processRawMediaPaths）
RAW_VIDEO_PATH_RE: re.Pattern = _build_raw_path_re("mp4|avi|mov|mkv|webm")
RAW_AUDIO_PATH_RE: re.Pattern = _build_raw_path_re("mp3|wav|flac|ogg|m4a|aac|amr")
RAW_FILE_PATH_RE: re.Pattern = _build_raw_path_re(
    "pdf|docx?|xlsx?|pptx?|zip|rar|txt|md|csv|json|xml|yaml|yml|toml|ini|cfg|log"
)



__all__ = [
    "IMAGE_EXTS",
    "AUDIO_EXTS",
    "VIDEO_EXTS",
    "ZIP_BEFORE_UPLOAD_EXTS",
    "TEXT_FILE_EXTENSIONS",
    "MEDIA_MSG_TYPES",
    "LOCAL_IMAGE_RE",
    "BARE_IMAGE_PATH_RE",
    "VIDEO_MARKER_RE",
    "AUDIO_MARKER_RE",
    "FILE_MARKER_RE",
    "RAW_VIDEO_PATH_RE",
    "RAW_AUDIO_PATH_RE",
    "RAW_FILE_PATH_RE",
]
