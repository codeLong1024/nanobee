"""
audit_logger 参考插件 —— turn / tool 两级 span 审计

本插件将原先的「计数日志」升级为结构化 span 审计：

- tool span：``on_pre_invoke`` 记录工具开始，``on_post_invoke`` 配对出工具
  完成 span（带原生 callId、耗时、isError、参数摘要）。
- turn span：``on_message_completed`` 产出整轮 span（含 token 汇总、
  finish_reason、工具调用次数、迭代次数）。

span 同时落两份：
1. 本地 JSONL：``<context_root>/audit_logger/<user_id>.jsonl``
   （context_root 未注入时回退到系统临时目录）
2. 结构化日志：通过 loguru 输出单行 JSON。

callId 使用框架透传的原生工具调用 ID（``ToolCallRequest.id``）：框架
``on_pre_invoke`` / ``on_post_invoke`` 钩子链已携带该 ID，插件据此做精确配对。
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.helpers import estimate_prompt_tokens
from nanobee.utils.logger import logger

# 工具结果过长时的截断长度
_ARG_MAX_CHARS = 200
_RESULT_MAX_CHARS = 120

# 判定工具结果是否为错误的标志（工具返回字符串时据此推断 isError）
_ERROR_MARKERS = ("error:", "exception:", "failed", "错误", "异常", "失败")


@dataclass
class ToolSpan:
    """单个工具调用的 span 记录。

    call_id 为框架透传的原生工具调用 ID（ToolCallRequest.id），
    用于在同一 turn 内将 on_pre_invoke / on_post_invoke 精确配对。
    """

    call_id: str
    tool_name: str
    start_time: float
    end_time: float | None = None
    duration_ms: float | None = None
    arg_preview: str = ""
    result_preview: str = ""
    is_error: bool = False
    interrupted: bool = False

    def close(self, result: Any) -> None:
        """结束 span，记录耗时与结果。"""
        self.end_time = time.perf_counter()
        self.duration_ms = round((self.end_time - self.start_time) * 1000, 3)
        self.result_preview = _preview(result, _RESULT_MAX_CHARS)
        self.is_error = _looks_like_error(result)


@dataclass
class TurnSpan:
    """整轮交互的 span 记录。"""

    type: str = "turn_span"
    turn_id: str = ""
    user_id: str = ""
    start_time: float = 0.0
    end_time: float | None = None
    duration_ms: float | None = None
    messages: int = 0
    iterations: int = 0
    tool_calls: int = 0
    tool_spans: list[ToolSpan] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""


@dataclass
class _TurnState:
    """单个用户的进行中 turn 状态（多请求并发安全）。"""

    span: TurnSpan
    counter: int = 0
    pending: list[ToolSpan] = field(default_factory=list)


def _preview(value: Any, max_chars: int) -> str:
    """将任意值转换为可读的截断预览文本。"""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def _looks_like_error(result: Any) -> bool:
    """启发式判断工具结果是否表示错误。

    框架的 on_post_invoke 仅在工具成功执行路径上触发；守卫拦截或异常时
    不会进入 on_post_invoke，那些场景的 span 会在 turn 关闭时标记 interrupted。
    此处仅针对「执行成功但返回了错误文本」的结果（如工具返回 "Error: ..."）。
    """
    text = result if isinstance(result, str) else ""
    lowered = text.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


class AuditLoggerPlugin(NanobeePlugin):
    """结构化审计日志插件：产出 turn / tool 两级 span。

    实现 ``on_pre_invoke`` / ``on_post_invoke`` / ``on_message_completed``
    三个 Hook，零贡献提示词与工具。
    """

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        self._call_count: dict[str, int] = {}
        # user_id -> 进行中 turn 状态
        self._turns: dict[str, _TurnState] = {}
        # user_id -> 已完成 turn span 列表（供测试断言）
        self._completed: dict[str, list[TurnSpan]] = {}

    # =========================================================================
    # 工具调用 span（on_pre_invoke / on_post_invoke）
    # =========================================================================

    async def on_pre_invoke(
        self,
        context: Any,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """工具执行前：以原生 call_id 开启一个 tool span，返回原始参数。"""
        user_id = _user_id(context)
        state = self._turns.get(user_id)
        if state is None:
            state = self._new_turn(user_id)
        span = ToolSpan(
            call_id=call_id or f"call_{state.counter + 1}",
            tool_name=tool_name,
            start_time=time.perf_counter(),
            arg_preview=_preview(args, _ARG_MAX_CHARS),
        )
        if not call_id:
            state.counter += 1
        state.span.tool_calls += 1
        state.pending.append(span)
        state.span.tool_spans.append(span)
        logger.debug(
            "[audit] tool-start user={} call={} tool={}",
            user_id, span.call_id, tool_name,
        )
        return args

    async def on_post_invoke(
        self,
        context: Any,
        call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        """工具执行后：按原生 call_id 配对 tool span 并结束，返回原始结果。"""
        user_id = _user_id(context)
        state = self._turns.get(user_id)
        if state is None:
            return result
        span = _pop_pending(state.pending, call_id, tool_name)
        if span is None:
            # 无配对（例如 guard 短路），不产生孤儿 span
            logger.debug(
                "[audit] tool-end 无配对 user={} call={} tool={}",
                user_id, call_id, tool_name,
            )
            return result
        span.close(result)
        logger.info(
            "[audit] tool-end user={} call={} tool={} duration={:.1f}ms isError={}",
            user_id, span.call_id, span.tool_name,
            span.duration_ms or 0.0, span.is_error,
        )
        return result

    # =========================================================================
    # 整轮 span（on_message_completed）
    # =========================================================================

    async def on_message_completed(
        self,
        context: Any,
        messages: list[dict[str, Any]],
    ) -> None:
        """记录本轮交互的 turn span 并持久化。

        统计本轮消息数、迭代次数、工具调用次数，估算 token 汇总与
        finish_reason，连同子 tool span 一并写入 JSONL 与结构化日志。
        ``call_count`` 计数保持原有语义（按 user_id 累计）。
        """
        user_id = _user_id(context)
        self._call_count[user_id] = self._call_count.get(user_id, 0) + 1

        state = self._turns.pop(user_id, None)
        if state is None:
            state = self._new_turn(user_id)
        span = state.span
        span.end_time = time.perf_counter()
        span.duration_ms = round((span.end_time - span.start_time) * 1000, 3)
        span.messages = len(messages)
        span.iterations = _count_iterations(messages)
        span.tool_calls = _count_tool_calls(messages)
        span.finish_reason = _infer_finish_reason(messages)

        # 估算 token 汇总（框架未把 usage 注入 messages，故用估算口径）
        span.prompt_tokens = _estimate_prompt_tokens(messages)
        span.completion_tokens = _estimate_completion_tokens(messages)

        # 未配对的 pending span 标记为 interrupted（守卫拦截或异常中断）
        for pending_span in state.pending:
            pending_span.interrupted = True
            pending_span.end_time = span.end_time
            pending_span.duration_ms = round(
                (pending_span.end_time - pending_span.start_time) * 1000, 3,
            )
            logger.warning(
                "[audit] tool-interrupted user={} call={} tool={}",
                user_id, pending_span.call_id, pending_span.tool_name,
            )

        self._completed.setdefault(user_id, []).append(span)
        self._write_turn(user_id, span)
        logger.info(
            "[audit] turn-end user={} round={} messages={} iterations={} "
            "tools={} prompt={} completion={} finish_reason={} duration={:.1f}ms",
            user_id, self._call_count[user_id], span.messages,
            span.iterations, span.tool_calls, span.prompt_tokens,
            span.completion_tokens, span.finish_reason, span.duration_ms or 0.0,
        )

    # =========================================================================
    # 持久化
    # =========================================================================

    def _write_turn(self, user_id: str, span: TurnSpan) -> None:
        """将 turn span 写入 JSONL 文件，并输出结构化日志。

        Args:
            user_id: 记录所属用户（用于 JSONL 文件名）。
            span: 待持久化的 turn span。
        """
        record = asdict(span)
        self._write_jsonl(user_id, record)

        # 结构化日志（单行 JSON）
        logger.info("[audit-json] {}", json.dumps(record, ensure_ascii=False))

    def _jsonl_path(self, user_id: str) -> Path:
        """解析 JSONL 输出路径。

        优先 ``<context_root>/audit_logger/<user_id>.jsonl``；
        context_root 未注入时回退到系统临时目录下的进程级文件，
        保证测试与无注入场景不写坏工作区。

        Args:
            user_id: 当前记录所属用户。

        Returns:
            JSONL 文件的绝对路径。
        """
        root = self.context_root
        base = Path(root) if root else Path(tempfile.gettempdir()) / "nanobee-audit"
        return base / "audit_logger" / f"{user_id or 'default'}.jsonl"

    def _write_jsonl(self, user_id: str, record: dict[str, Any]) -> None:
        """以追加模式将一条记录写入 JSONL。

        Args:
            user_id: 当前记录所属用户。
            record: 待写入的 JSON 记录。
        """
        path = self._jsonl_path(user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("[audit] 写入 JSONL 失败: {}", path)

    # =========================================================================
    # 内部状态辅助
    # =========================================================================

    def _new_turn(self, user_id: str) -> _TurnState:
        state = _TurnState(
            span=TurnSpan(
                turn_id=f"turn_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                start_time=time.perf_counter(),
            ),
        )
        self._turns[user_id] = state
        return state

    # =========================================================================
    # 测试断言辅助
    # =========================================================================

    @property
    def call_count(self) -> int:
        """获取所有用户的累计被调用次数，用于测试验证。"""
        return sum(self._call_count.values())

    def completed_spans(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """获取已完成 turn span 的 dict 列表，用于测试断言。

        Args:
            user_id: 过滤指定用户；None 返回全部用户的 span。

        Returns:
            turn span 的 dict 列表（含嵌套 tool span）。
        """
        if user_id is not None:
            return [asdict(s) for s in self._completed.get(user_id, [])]
        return [asdict(s) for spans in self._completed.values() for s in spans]

    def tool_spans(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """获取已记录 tool span 的 dict 列表（含进行中与已完成的）。"""
        spans: list[ToolSpan] = []
        for u, state in self._turns.items():
            if user_id is None or u == user_id:
                spans.extend(state.span.tool_spans)
        for u, completed in self._completed.items():
            if user_id is None or u == user_id:
                for s in completed:
                    spans.extend(s.tool_spans)
        return [asdict(s) for s in spans]


# =============================================================================
# 辅助函数
# =============================================================================


def _user_id(context: Any) -> str:
    """从 context 提取 user_id，缺失时回退为 'default'。"""
    value = getattr(context, "user_id", None)
    return value if isinstance(value, str) and value else "default"


def _count_tool_calls(messages: list[dict[str, Any]]) -> int:
    """统计消息列表中的工具调用总数。"""
    return sum(
        1
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    )


def _count_iterations(messages: list[dict[str, Any]]) -> int:
    """统计 assistant 消息条数（近似 LLM 迭代次数）。"""
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def _infer_finish_reason(messages: list[dict[str, Any]]) -> str:
    """从消息列表推断结束原因。

    框架未把 finish_reason 注入 messages（P1 约束），此处基于最后一条
    assistant 消息是否仍携带 tool_calls 做启发式推断。
    """
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            if m.get("tool_calls"):
                return "tool_call"
            return "completed"
    return "completed"


def _estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """估算本轮 prompt token 数（复用框架的估算工具）。"""
    return estimate_prompt_tokens(messages)


def _estimate_completion_tokens(messages: list[dict[str, Any]]) -> int:
    """估算本轮 completion token 数（基于 assistant 内容字符长度）。"""
    total_chars = 0
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str):
                total_chars += len(content)
    return total_chars // 4


def _pop_pending(
    pending: list[ToolSpan],
    call_id: str,
    tool_name: str,
) -> ToolSpan | None:
    """从 pending 中弹出与给定 call_id 匹配的 tool span。

    优先按 call_id 精确匹配（框架透传的原生 ID 唯一）；call_id 为空时
    回退为按 tool_name 匹配最早的 span（兼容无 ID 注入的测试场景）。

    Args:
        pending: 进行中 turn 的待配对 span 列表。
        call_id: 待匹配的原生工具调用 ID。
        tool_name: 待匹配的工具名称（call_id 为空时的回退依据）。

    Returns:
        匹配到的 span；无匹配返回 None。
    """
    if call_id:
        for span in pending:
            if span.call_id == call_id:
                pending.remove(span)
                return span
        return None
    for span in pending:
        if span.tool_name == tool_name:
            pending.remove(span)
            return span
    return None
