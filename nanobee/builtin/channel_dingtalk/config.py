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

    # ============ 媒体配置（以下三项已接线，配置即生效）============
    # enable_media_upload: 媒体读取总开关；false 时本地与远端附件均拒绝读取。
    # media_max_mb: 远端与本地附件共用的体积上限（MB，>= 1；本地读取超限即中止）。
    # media_local_roots: 本地附件白名单根目录（额外放行；相对路径按 data_dir 解析）。
    #   data_dir 与入站附件目录（./media/dingtalk）始终放行。
    enable_media_upload: bool = True
    media_max_mb: int = Field(default=20, ge=1)
    media_local_roots: list[str] = Field(default_factory=list)

    # ⚠️ 以下两项尚未接线（配置后不生效）——注意：分片上传本身**已实现**
    # （media/upload.py 按 20MB 阈值自动切换到分片协议），但其阈值/块大小目前
    # 是代码内常量（CHUNK_THRESHOLD / CHUNK_DEFAULT），不受这两个字段驱动。
    enable_chunk_upload: bool = True
    chunk_size_kb: int = 5120

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
    # 卡片流式推送最小间隔（秒）。LLM 每个 SSE delta 都会触发一次
    # /card/streaming 全量 PUT，逐帧推送会钳制解码速度。设为 >0 后，
    # 间隔内的增量只累积到 buffer，由 _stream_end 终态全量推送兜底；
    # 首帧始终立即推送。0 = 逐 delta 推送（旧行为，用于回退）。
    stream_push_min_interval: float = 1.0

    # ============ 消息标题配置 ============
    markdown_title: str = "智能体回复"
