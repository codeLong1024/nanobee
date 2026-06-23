"""DingTalk rich media constants — ported from services/media/common.ts + utils/constants.ts.

Provides:
- Extension sets for images, audio, video
- Compiled regex patterns for media markers and path detection
- ``guess_upload_type()`` for inferring DingTalk upload type from file extension
"""

from __future__ import annotations

import re

from .helpers import (
    IMAGE_EXTS,
    AUDIO_EXTS,
    VIDEO_EXTS,
    ZIP_BEFORE_UPLOAD_EXTS,
)

# Backward-compatible alias
IMAGE_EXTENSIONS: set[str] = IMAGE_EXTS

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

# 匹配 Markdown 中的本地图片: ![alt](path)
LOCAL_IMAGE_RE: re.Pattern = re.compile(
    r'!\[([^\]]*)\]\(([^)]+)\)'
)

# 匹配裸露的本地图片路径（绝对路径）
# 同时支持 Unix (/path/file) 和 Windows (C:\path\file 或 C:/path/file) 路径
BARE_IMAGE_PATH_RE: re.Pattern = re.compile(
    r'(?<![(\[:/])'
    r'('
    r'/' + _PATH_CHARS + r'\.(?:png|jpg|jpeg|gif|webp|bmp|svg)'
    r'|'
    r'[A-Za-z]:[/\\]' + _PATH_CHARS + r'\.(?:png|jpg|jpeg|gif|webp|bmp|svg)'
    r')'
    r'(?![)\]])',
    re.IGNORECASE,
)

# 视频标记: [DINGTALK_VIDEO]{"path":"..."}[/DINGTALK_VIDEO]
VIDEO_MARKER_RE: re.Pattern = re.compile(
    r'\[DINGTALK_VIDEO\](.*?)\[/DINGTALK_VIDEO\]',
    re.DOTALL,
)

# 音频标记: [DINGTALK_AUDIO]{"path":"..."}[/DINGTALK_AUDIO]
AUDIO_MARKER_RE: re.Pattern = re.compile(
    r'\[DINGTALK_AUDIO\](.*?)\[/DINGTALK_AUDIO\]',
    re.DOTALL,
)

# 文件标记: [DINGTALK_FILE]{"path":"...","fileName":"...","fileType":"..."}[/DINGTALK_FILE]
FILE_MARKER_RE: re.Pattern = re.compile(
    r'\[DINGTALK_FILE\](.*?)\[/DINGTALK_FILE\]',
    re.DOTALL,
)

# 裸露的媒体路径（用于 processRawMediaPaths）
# 同时支持 Unix (/path/file) 和 Windows (C:\path\file 或 C:/path/file) 路径。

RAW_VIDEO_PATH_RE: re.Pattern = re.compile(
    r'(?<![(\[:/])'
    r'('
    r'/' + _PATH_CHARS + r'\.(?:mp4|avi|mov|mkv|webm)'
    r'|'
    r'[A-Za-z]:[/\\]' + _PATH_CHARS + r'\.(?:mp4|avi|mov|mkv|webm)'
    r')'
    r'(?![)\]])',
    re.IGNORECASE,
)
RAW_AUDIO_PATH_RE: re.Pattern = re.compile(
    r'(?<![(\[:/])'
    r'('
    r'/' + _PATH_CHARS + r'\.(?:mp3|wav|flac|ogg|m4a|aac|amr)'
    r'|'
    r'[A-Za-z]:[/\\]' + _PATH_CHARS + r'\.(?:mp3|wav|flac|ogg|m4a|aac|amr)'
    r')'
    r'(?![)\]])',
    re.IGNORECASE,
)
RAW_FILE_PATH_RE: re.Pattern = re.compile(
    r'(?<![(\[:/])'
    r'('
    r'/' + _PATH_CHARS + r'\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|txt|md|csv|json|xml|yaml|yml|toml|ini|cfg|log)'
    r'|'
    r'[A-Za-z]:[/\\]' + _PATH_CHARS + r'\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|txt|md|csv|json|xml|yaml|yml|toml|ini|cfg|log)'
    r')'
    r'(?![)\]])',
    re.IGNORECASE,
)


from .helpers import guess_upload_type  # noqa: F811 — re-export from helpers


__all__ = [
    "IMAGE_EXTS",
    "AUDIO_EXTS",
    "VIDEO_EXTS",
    "IMAGE_EXTENSIONS",
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
    "guess_upload_type",
]
