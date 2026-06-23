"""DingTalk rich media processing subpackage.

Ported from dingtalk-openclaw-connector services/media/ (v0.8.20).

Submodules:
- constants: Regex patterns, extension maps, media type inference
- helpers: Pure helper functions for media handling
- fetch: SSRF-protected remote media fetching
- upload: Normal + chunked upload to DingTalk
- download: Inbound download with content-type extension inference
- markers: [DINGTALK_VIDEO/AUDIO/FILE] marker processing
- image_processor: Markdown image path replacement + bare image paths
- raw_path: Bare media path detection (safety net)
- file_parser: File content parsing (docx/pdf/text)
"""

from .constants import (
    IMAGE_EXTENSIONS,
    TEXT_FILE_EXTENSIONS,
    MEDIA_MSG_TYPES,
    LOCAL_IMAGE_RE,
    BARE_IMAGE_PATH_RE,
    VIDEO_MARKER_RE,
    AUDIO_MARKER_RE,
    FILE_MARKER_RE,
    RAW_VIDEO_PATH_RE,
    RAW_AUDIO_PATH_RE,
    RAW_FILE_PATH_RE,
    guess_upload_type,
)
from .helpers import (
    IMAGE_EXTS,
    AUDIO_EXTS,
    VIDEO_EXTS,
    ZIP_BEFORE_UPLOAD_EXTS,
)

__all__ = [
    "IMAGE_EXTENSIONS",
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
    "guess_upload_type",
]
