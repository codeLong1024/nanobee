"""可观测性工具：结构化日志、Trace ID、Metrics 采集。

日志架构（分层）：
    Layer 1: 审计日志 — 插件策略层（audit_logger Hook），业务记录
    Layer 2: 运行时日志 — 程序自管理（loguru 文件 sink），运维排障
    Layer 3: Shell 重定向 — stdout/stderr 兜底捕获
"""

from __future__ import annotations

import logging
import random
import string
import sys
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def generate_trace_id() -> str:
    """生成 Trace ID（32 位十六进制字符串）。"""
    return f"{time.time_ns():x}{''.join(random.choices(string.hexdigits.lower(), k=8))}"


def set_trace_id(tid: str | None) -> None:
    """设置当前协程的 Trace ID。"""
    _trace_id_var.set(tid)


def get_trace_id() -> str | None:
    """获取当前协程的 Trace ID。"""
    return _trace_id_var.get()


def reset_trace_id() -> None:
    """重置当前协程的 Trace ID。"""
    _trace_id_var.set(None)


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
