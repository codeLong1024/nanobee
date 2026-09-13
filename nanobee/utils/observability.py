"""可观测性工具：结构化日志、Trace ID、Metrics 采集。

日志架构（分层）：
    Layer 1: 审计日志 — 插件策略层（audit_logger Hook），业务记录
    Layer 2: 运行时日志 — 程序自管理（loguru 文件 sink），运维排障
    Layer 3: Shell 重定向 — stdout/stderr 兜底捕获
"""

from __future__ import annotations

import logging
import re
import secrets
import sys
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

# ---------------------------------------------------------------------------
# Trace / Span ID（对齐 W3C Trace Context 规范，不引入 opentelemetry 依赖）
# ---------------------------------------------------------------------------
# Trace ID：32 位小写十六进制（128 bit），不得全零
# Span ID：16 位小写十六进制（64 bit），不得全零
# 规范依据：https://www.w3.org/TR/trace-context/

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_parent_span_id_var: ContextVar[str | None] = ContextVar("parent_span_id", default=None)

_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")
_SPAN_ID_RE = re.compile(r"[0-9a-f]{16}")
_ALL_ZERO_TRACE_ID = "0" * 32
_ALL_ZERO_SPAN_ID = "0" * 16


def is_valid_trace_id(tid: object) -> bool:
    """校验 Trace ID 是否符合 W3C 规范。

    Args:
        tid: 待校验值（接受任意对象，非字符串直接返回 False）

    Returns:
        32 位十六进制且非全零（大小写均可）返回 True，否则 False
    """
    if not isinstance(tid, str):
        return False
    lowered = tid.lower()
    return bool(_TRACE_ID_RE.fullmatch(lowered)) and lowered != _ALL_ZERO_TRACE_ID


def is_valid_span_id(sid: object) -> bool:
    """校验 Span ID 是否符合 W3C 规范。

    Args:
        sid: 待校验值（接受任意对象，非字符串直接返回 False）

    Returns:
        16 位十六进制且非全零（大小写均可）返回 True，否则 False
    """
    if not isinstance(sid, str):
        return False
    lowered = sid.lower()
    return bool(_SPAN_ID_RE.fullmatch(lowered)) and lowered != _ALL_ZERO_SPAN_ID


def generate_trace_id() -> str:
    """生成 Trace ID（严格 32 位小写十六进制，非全零）。

    使用 CSPRNG（``secrets``）保证唯一性质量；不携带时间分段——
    时序信息由 span timestamp 承担，对齐 W3C/OTel 标准（纯随机 128 bit）。
    """
    while True:
        tid = secrets.token_hex(16)
        # 全零概率 2^-128，此分支仅为对齐规范的形式校验
        if tid != _ALL_ZERO_TRACE_ID:
            return tid


def generate_span_id() -> str:
    """生成 Span ID（严格 16 位小写十六进制，非全零）。"""
    while True:
        sid = secrets.token_hex(8)
        # 全零概率 2^-64，此分支仅为对齐规范的形式校验
        if sid != _ALL_ZERO_SPAN_ID:
            return sid


def set_trace_id(tid: str | None) -> None:
    """设置当前协程的 Trace ID。

    非 None 的非法值直接拒绝（raise），防止垃圾 id 污染日志串联；
    外部来源的 id 应在调用边界先用 :func:`is_valid_trace_id` 归一化。

    Args:
        tid: Trace ID，None 表示清除

    Raises:
        ValueError: tid 非 None 且不符合 W3C 规范
    """
    if tid is not None and not is_valid_trace_id(tid):
        raise ValueError(f"非法 Trace ID: {tid!r}")
    _trace_id_var.set(tid)


def get_trace_id() -> str | None:
    """获取当前协程的 Trace ID。"""
    return _trace_id_var.get()


def reset_trace_id() -> None:
    """重置当前协程的 Trace ID。"""
    _trace_id_var.set(None)


def set_parent_span_id(sid: str | None) -> None:
    """设置当前协程的父 Span ID。

    Args:
        sid: Span ID，None 表示清除

    Raises:
        ValueError: sid 非 None 且不符合 W3C 规范
    """
    if sid is not None and not is_valid_span_id(sid):
        raise ValueError(f"非法 Span ID: {sid!r}")
    _parent_span_id_var.set(sid)


def get_parent_span_id() -> str | None:
    """获取当前协程的父 Span ID。"""
    return _parent_span_id_var.get()


def reset_parent_span_id() -> None:
    """重置当前协程的父 Span ID。"""
    _parent_span_id_var.set(None)


# ---------------------------------------------------------------------------
# OTLP 协议常量（纯数值定义，无第三方依赖）
# ---------------------------------------------------------------------------


class SpanKind(IntEnum):
    """Span 种类（OTLP proto 数值，非字符串）。

    数值对齐 opentelemetry/proto/trace/v1/trace.proto 的 Span.SpanKind。
    """

    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCode(IntEnum):
    """Span 状态码（OTLP proto 数值）。

    数值对齐 opentelemetry/proto/trace/v1/trace.proto 的 Status.StatusCode。
    """

    UNSET = 0
    OK = 1
    ERROR = 2


# 常用属性键（OTel 语义约定风格）。仅登记框架自身埋点可复用的键，
# 新键随真实调用方按需追加，不预设完整语义分类学。
ATTR_SESSION_ID = "session.id"
ATTR_CHANNEL = "channel.name"
ATTR_CHAT_ID = "chat.id"
ATTR_SENDER_ID = "sender.id"
ATTR_TOOL_NAME = "tool.name"
ATTR_TURN_ID = "turn.id"


def now_unix_nano() -> int:
    """当前 Unix 时间（纳秒整数），OTLP timestamp 字段用。"""
    return time.time_ns()


# ---------------------------------------------------------------------------
# W3C traceparent 格式化 / 解析（纯函数，无 ContextVar 副作用）
# ---------------------------------------------------------------------------

_TRACEPARENT_VERSION = "00"
_FLAGS_RE = re.compile(r"[0-9a-f]{2}")


def format_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    """格式化为 W3C traceparent 头：``00-<trace_id>-<span_id>-<flags>``。

    Args:
        trace_id: 32 位十六进制 Trace ID
        span_id: 16 位十六进制 Span ID
        flags: 2 位十六进制标志位，默认 ``"01"``（sampled）

    Returns:
        小写规范形式的 traceparent 字符串

    Raises:
        ValueError: 任一入参不符合规范
    """
    if not is_valid_trace_id(trace_id):
        raise ValueError(f"非法 Trace ID: {trace_id!r}")
    if not is_valid_span_id(span_id):
        raise ValueError(f"非法 Span ID: {span_id!r}")
    if not _FLAGS_RE.fullmatch(flags):
        raise ValueError(f"非法 flags: {flags!r}")
    return f"{_TRACEPARENT_VERSION}-{trace_id.lower()}-{span_id.lower()}-{flags.lower()}"


def parse_traceparent(value: str) -> tuple[str, str, str] | None:
    """解析 W3C traceparent 头（纯函数，不做任何 ContextVar 副作用）。

    Args:
        value: traceparent 字符串

    Returns:
        ``(trace_id, span_id, flags)`` 小写三元组；非法输入返回 None。
    """
    if not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or parts[0] != _TRACEPARENT_VERSION:
        return None
    trace_id, span_id, flags = parts[1].lower(), parts[2].lower(), parts[3].lower()
    if not is_valid_trace_id(trace_id) or not is_valid_span_id(span_id):
        return None
    if not _FLAGS_RE.fullmatch(flags):
        return None
    return trace_id, span_id, flags


# 需要抑制噪音的第三方日志命名空间列表
_NOISY_LOGGERS: list[str] = [
    "websockets",
    "websockets.client",
    "websockets.server",
    "websockets.protocol",
    "dingtalk_stream",
    "dingtalk_stream.stream",
    "httpx",
    "httpx._client",
    "httpcore",
    "urllib3",
    "charset_normalizer",
]


def _suppress_noisy_loggers() -> None:
    """将第三方噪音日志级别提升到 WARNING，避免 DEBUG/INFO 刷屏。"""
    for name in _NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.WARNING)


class TraceIDFilter(logging.Filter):
    """为所有日志记录注入 trace_id 字段。"""
    
    def filter(self, record: logging.LogRecord) -> bool:
        tid = get_trace_id()
        record.trace_id = tid or "-"
        return True


def setup_structured_logging(
    level: int = logging.INFO,
    json_output: bool = False,
) -> None:
    """配置结构化日志（使用 loguru）。
    
    Args:
        level: 日志级别
        json_output: 是否输出 JSON 格式（生产环境用 True）
    """
    # 清除已有的 handler，避免重复添加
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    
    root.addFilter(TraceIDFilter())
    
    # 抑制第三方噪音日志
    _suppress_noisy_loggers()
    
    # 移除 loguru 默认的 handler
    _loguru_logger.remove()
    
    if json_output:
        # JSON 格式（生产环境）
        _loguru_logger.add(
            sys.stderr,
            format="{message}",
            serialize=True,
            level=logging.getLevelName(level).upper(),
        )
    else:
        # 自定义格式（开发环境）
        _loguru_logger.add(
            sys.stderr,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | <level>{level:<8}</level> | {name}:{function}:{line} - {message}",
            level=logging.getLevelName(level).upper(),
            colorize=True,
            enqueue=True,
            diagnose=False,
        )
    
    # 将 loguru 适配为标准 logging
    logging.getLogger().setLevel(level)


def init_log_file_sink(
    log_cfg: dict[str, Any] | None = None,
    instance_name: str | None = None,
) -> None:
    """根据配置添加 loguru 文件 sink（运行时日志自管理）。

    从 ``logging:`` 配置段读取文件路径、轮转策略、保留策略等，
    自动添加 loguru 文件 sink。如果 ``file`` 未配置则静默跳过。

    支持 ``{instance}`` 占位符，多实例部署时传入不同实例名实现日志分散。

    Args:
        log_cfg: logging 配置字典，对应 LoggingConfig.model_dump()
        instance_name: 实例名，用于替换 ``file`` 中的 ``{instance}`` 占位符
    """
    if not log_cfg:
        return

    log_file = log_cfg.get("file")
    if not log_file:
        return

    # 替换实例名占位符
    if instance_name is not None:
        log_file = log_file.replace("{instance}", instance_name)

    log_dir = log_cfg.get("dir", "logs")
    log_path = Path(log_dir) / log_file

    _loguru_logger.add(
        str(log_path),
        rotation=log_cfg.get("rotation", "500 MB"),
        retention=log_cfg.get("retention", "30 days"),
        compression=log_cfg.get("compression", "gz"),
        level=log_cfg.get("level", "INFO"),
        enqueue=True,
        serialize=log_cfg.get("json_format", False),
    )


@dataclass
class MetricsSnapshot:
    """指标快照。"""
    total_turns: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    error_count: int = 0
    tool_invocations: int = 0
    avg_latency_ms: float = 0.0
    latency_samples: list[float] = field(default_factory=list)
    error_kinds: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # type: ignore[arg-type]
    tool_stats: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"calls": 0, "errors": 0})  # type: ignore[arg-type]
    )


class MetricsCollector:
    """指标采集器（进程内聚合）。

    支持 Token 消耗、延迟分布、工具调用、错误计数等关键指标。
    """
    
    def __init__(self) -> None:
        self._snapshot = MetricsSnapshot()
    
    def record_token_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """记录 Token 消耗。

        Args:
            prompt_tokens: 输入 Token 数
            completion_tokens: 输出 Token 数
        """
        self._snapshot.total_prompt_tokens += prompt_tokens
        self._snapshot.total_completion_tokens += completion_tokens
    
    def record_latency(self, duration_ms: float) -> None:
        """记录操作延迟（滚动窗口，最多 1000 样本）。

        Args:
            duration_ms: 延迟毫秒数
        """
        samples = self._snapshot.latency_samples
        samples.append(duration_ms)
        if len(samples) > 1000:
            samples.pop(0)
        self._snapshot.avg_latency_ms = sum(samples) / len(samples)
    
    def record_tool_invocation(self, tool_name: str, success: bool) -> None:
        """记录工具调用。

        Args:
            tool_name: 工具名称
            success: 是否成功
        """
        self._snapshot.tool_invocations += 1
        stats = self._snapshot.tool_stats[tool_name]
        stats["calls"] += 1
        if not success:
            stats["errors"] += 1
    
    def record_error(self, error_kind: str) -> None:
        """记录错误。

        Args:
            error_kind: 错误类型
        """
        self._snapshot.error_count += 1
        self._snapshot.error_kinds[error_kind] += 1
    
    def record_turn(self) -> None:
        """记录一个完整的 Turn。"""
        self._snapshot.total_turns += 1
    
    def get_report(self) -> dict[str, Any]:
        """获取指标报告。

        Returns:
            包含所有聚合指标的字典
        """
        s = self._snapshot
        return {
            "total_turns": s.total_turns,
            "total_prompt_tokens": s.total_prompt_tokens,
            "total_completion_tokens": s.total_completion_tokens,
            "total_tokens": s.total_prompt_tokens + s.total_completion_tokens,
            "error_count": s.error_count,
            "tool_invocations": s.tool_invocations,
            "avg_latency_ms": round(s.avg_latency_ms, 2),
            "error_kinds": dict(s.error_kinds),
            "tool_stats": {k: dict(v) for k, v in s.tool_stats.items()},
        }
    
    def reset(self) -> None:
        """重置所有指标。"""
        self._snapshot = MetricsSnapshot()
