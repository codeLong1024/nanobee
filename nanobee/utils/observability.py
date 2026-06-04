"""可观测性工具：结构化日志、Trace ID、Metrics 采集。"""

from __future__ import annotations

import logging
import random
import string
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

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


class TraceIDFilter(logging.Filter):
    """为所有日志记录注入 trace_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = get_trace_id()
        record.trace_id = tid or "-"
        return True


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


def setup_structured_logging(
    level: int = logging.INFO,
    json_output: bool = False,
) -> None:
    """配置结构化日志。

    Args:
        level: 日志级别
        json_output: 是否输出 JSON 格式（生产环境用 True）
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有的 handler，避免重复添加
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.addFilter(TraceIDFilter())

    # 抑制第三方噪音日志
    _suppress_noisy_loggers()

    # 尝试使用 structlog 库
    # 当前依赖中未包含 structlog，此处保留元能力，待依赖就绪后激活
    _try_structlog(level, json_output)

    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)


def _try_structlog(level: int, json_output: bool) -> None:
    """尝试使用 structlog 配置日志格式。"""
    try:
        import structlog  # type: ignore[import-untyped]

        def _add_trace_id(logger, method_name, event_dict):
            """structlog processor：注入 trace_id。"""
            event_dict["trace_id"] = get_trace_id() or "-"
            return event_dict

        # 配置 structlog 处理器链（不包含最终渲染器）
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                _add_trace_id,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.stdlib.add_logger_name,
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        root = logging.getLogger()
        root.handlers.clear()
        handler = logging.StreamHandler()
        # ProcessorFormatter 作为最终渲染器桥接到标准 logging
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer()
                if json_output
                else structlog.dev.ConsoleRenderer(),
            )
        )
        root.addHandler(handler)
    except ImportError:
        # structlog 未安装，降级为标准日志
        pass


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
