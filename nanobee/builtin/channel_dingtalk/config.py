"""DingTalk channel configuration and constants for nanobee."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DingTalkConfig(BaseModel):
    """DingTalk channel configuration using Stream mode.

    日志级别不在此配置，自动跟随全局日志系统（logging root level）。
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    streaming: bool = True
    allow_from: list[str] = Field(default_factory=list)
    allow_remote_media_redirects: bool = False
    remote_media_redirect_allowed_hosts: list[str] = Field(default_factory=list)

    # ============ 媒体上传配置 ============
    enable_media_upload: bool = True
    media_max_mb: int = 20
    enable_chunk_upload: bool = True
    chunk_size_kb: int = 5120
    media_local_roots: list[str] = Field(default_factory=list)

    # ============ 文件解析配置 ============
    enable_file_parsing: bool = False
    max_file_parse_chars: int = 2000

    # ============ AI Agent 标记处理配置 ============
    enable_marker_processing: bool = True
    enable_video_thumbnail: bool = True

    # ============ 代理配置 ============
    proxy_url: str | None = None

    # ============ 流式输出配置 ============
    stream_buffer_max_chars: int = 500_000

    # ============ 消息标题配置 ============
    markdown_title: str = "智能体回复"
