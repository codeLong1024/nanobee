"""
audit_logger 参考插件 —— turn / tool 两级 span 审计

本插件将原先的「计数日志」升级为结构化 span 审计：

- tool span：``on_pre_invoke`` 记录工具开始，``on_post_invoke`` 配对出工具
  完成 span（带原生 callId、耗时、status、参数摘要）。
- turn span：``on_message_completed`` 产出整轮 span（含 token 汇总、
  finish_reason、工具调用次数、迭代次数、用户输入原文与最终回复预览）。

**数据契约（v1）**：JSONL 输出的字段命名对齐 OTel GenAI Semantic Conventions：

- ``gen_ai.*``：严格采用 OTel GenAI 语义约定属性命名。
- ``nanobee.*``：框架自有概念（截断标记、估算标记、内部统计）。
- 无前缀通用字段（``schema``/``trace_id``/``start_time``/``end_time`` 等）：
  通用 span 语义或契约元数据。

一行 = 一个终态 turn（一次用户请求到最终回复的完整链路）。
``perf_counter`` 单调时钟仅在进程内用于计算 ``duration_ms``，不落盘。
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.helpers import estimate_prompt_tokens, strip_runtime_context
from nanobee.utils.logger import logger

# 工具参数/结果的默认截断长度（AuditLoggerConfig 字段默认值）
_ARG_MAX_CHARS = 2000
_RESULT_MAX_CHARS = 2000
# turn 内容侧字段的默认截断长度（用户输入原文 / 最终回复预览）
_USER_MAX_CHARS = 500
_REPLY_MAX_CHARS = 800

# 判定工具结果是否为错误的标志（工具返回字符串时据此推断 isError）
_ERROR_MARKERS = ("error:", "exception:", "failed", "错误", "异常", "失败")

# OTel GenAI operation name 固定值
_TURN_OPERATION = "invoke_agent"
_TOOL_OPERATION = "execute_tool"

# 契约版本标识
_SCHEMA = "nanobee.audit/1"


class AuditLoggerConfig(BaseModel):
    """audit_logger 插件声明式配置。

    框架在 initialize 阶段统一 model_validate，完成类型强转（``"false"``
    → ``False``）、约束校验（``ge=1``）与默认值填充；非法值自动降级为
    默认值，不阻塞框架启动。

    Attributes:
        agent_name: gen_ai.agent.name 属性值（OTel；多 agent 场景必须唯一）。
        preview_truncate: 截断总开关；false 时全量记录（测试/联调临时开启）。
        arg_max_chars: 参数预览截断长度上限（正整数）。
        result_max_chars: 结果预览截断长度上限（正整数）。
        user_max_chars: turn 记录用户输入原文截断长度上限（正整数）。
        reply_max_chars: turn 记录最终回复预览截断长度上限（正整数）。
    """

    agent_name: str = "nanobee"
    preview_truncate: bool = True
    arg_max_chars: int = Field(default=_ARG_MAX_CHARS, ge=1)
    result_max_chars: int = Field(default=_RESULT_MAX_CHARS, ge=1)
    user_max_chars: int = Field(default=_USER_MAX_CHARS, ge=1)
    reply_max_chars: int = Field(default=_REPLY_MAX_CHARS, ge=1)


@dataclass
class ToolSpan:
    """单个工具调用的 span 记录（OTel GenAI 契约命名）。

    ``span_id`` 为框架透传的原生工具调用 ID（ToolCallRequest.id），空则
    回退生成；用于在同一 turn 内将 on_pre_invoke / on_post_invoke 精确配对。
    ``_pc_start`` 为进程内 perf_counter 起点，仅用于计算 duration_ms，不落盘。
    """

    span_id: str = ""
    tool_name: str = ""                    # gen_ai.tool.name
    start_time: str = ""                   # ISO 墙钟（was ts_start_iso）
    end_time: str = ""                     # ISO 墙钟（was ts_end_iso）
    duration_ms: float | None = None
    arg_preview: str = ""                  # gen_ai.tool.call.arguments
    arg_truncated: bool = False            # nanobee.arguments.truncated
    result_preview: str = ""               # gen_ai.tool.call.result
    result_truncated: bool = False         # nanobee.result.truncated
    status: str = "unset"                  # "ok" / "error" / "unset"
    interrupted: bool = False              # nanobee.interrupted

    # 进程内内部计时起点（不落盘，repr=False 排除调试噪音）
    _pc_start: float = field(default=0.0, repr=False, compare=False)

    def close(self, result: Any, result_max: int | None) -> None:
        """结束 span，记录耗时与结果。

        Args:
            result: 工具执行结果（任意类型）。
            result_max: 结果预览截断长度上限（来自 config.result_max_chars
                与 preview_truncate 开关）；None 表示不截断。调用方必须
                显式传入，禁止默认值绕过配置。
        """
        self.end_time = _iso_now()
        self.duration_ms = round(
            (time.perf_counter() - self._pc_start) * 1000, 3,
        )
        self.result_preview, self.result_truncated = _preview(result, result_max)
        if _looks_like_error(result):
            self.status = "error"
        elif self.status == "unset":
            self.status = "ok"

    def to_contract_dict(self) -> dict[str, Any]:
        """序列化为 OTel GenAI 契约命名的 flat dict。"""
        return {
            "span_id": self.span_id,
            "gen_ai.operation.name": _TOOL_OPERATION,
            "gen_ai.tool.name": self.tool_name,
            "gen_ai.tool.call.id": self.span_id,
            "gen_ai.tool.call.arguments": self.arg_preview,
            "gen_ai.tool.call.result": self.result_preview,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "nanobee.interrupted": self.interrupted,
            "nanobee.arguments.truncated": self.arg_truncated,
            "nanobee.result.truncated": self.result_truncated,
        }


@dataclass
class TurnSpan:
    """整轮交互的 span 记录（OTel GenAI 契约命名）。

    turn = 一条 trace：``trace_id`` 为 turn 唯一标识。
    消息结构简化：``input_messages`` / ``output_messages`` 以
    ``[{"role", "content"}]`` 简化格式存储（OTel 完整 parts 结构由 bridge 组装）。
    ``_pc_start`` 为进程内 perf_counter 起点，仅用于计算 duration_ms，不落盘。
    """

    schema: str = _SCHEMA
    trace_id: str = ""                     # turn_{uuid12}
    operation_name: str = _TURN_OPERATION  # gen_ai.operation.name
    agent_name: str = "nanobee"            # gen_ai.agent.name（配置项）
    conversation_id: str = "default"       # gen_ai.conversation.id
    start_time: str = ""                   # ISO 墙钟（was ts_start_iso）
    end_time: str = ""                     # ISO 墙钟（was ts_end_iso）
    duration_ms: float | None = None
    input_tokens: int = 0                  # gen_ai.usage.input_tokens
    output_tokens: int = 0                 # gen_ai.usage.output_tokens
    total_tokens: int = 0                  # gen_ai.usage.total_tokens
    usage_estimated: bool = True           # nanobee.usage.estimated
    finish_reasons: list[str] = field(default_factory=list)
    # gen_ai.response.finish_reasons
    input_messages: list[dict] = field(default_factory=list)
    # gen_ai.input.messages: [{role, content}]
    output_messages: list[dict] = field(default_factory=list)
    # gen_ai.output.messages: [{role, content}]
    input_truncated: bool = False          # nanobee.input.truncated
    output_truncated: bool = False         # nanobee.output.truncated
    iterations: int = 0                    # nanobee.iterations
    message_count: int = 0                 # nanobee.messages
    tool_calls: int = 0                    # nanobee.tool_calls
    tool_spans: list[ToolSpan] = field(default_factory=list)

    # 进程内内部计时起点（不落盘）
    _pc_start: float = field(default=0.0, repr=False, compare=False)

    def to_contract_dict(self) -> dict[str, Any]:
        """序列化为 OTel GenAI 契约命名的 flat dict（含嵌套 tool_spans）。"""
        return {
            "schema": self.schema,
            "trace_id": self.trace_id,
            "gen_ai.operation.name": self.operation_name,
            "gen_ai.agent.name": self.agent_name,
            "gen_ai.conversation.id": self.conversation_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "gen_ai.usage.input_tokens": self.input_tokens,
            "gen_ai.usage.output_tokens": self.output_tokens,
            "gen_ai.usage.total_tokens": self.total_tokens,
            "nanobee.usage.estimated": self.usage_estimated,
            "gen_ai.response.finish_reasons": self.finish_reasons,
            "gen_ai.input.messages": self.input_messages,
            "gen_ai.output.messages": self.output_messages,
            "nanobee.input.truncated": self.input_truncated,
            "nanobee.output.truncated": self.output_truncated,
            "nanobee.iterations": self.iterations,
            "nanobee.messages": self.message_count,
            "nanobee.tool_calls": self.tool_calls,
            "tool_spans": [s.to_contract_dict() for s in self.tool_spans],
        }


@dataclass
class _TurnState:
    """单个用户的进行中 turn 状态（多请求并发安全）。"""

    span: TurnSpan
    counter: int = 0
    pending: list[ToolSpan] = field(default_factory=list)


def _preview(value: Any, max_chars: int | None) -> tuple[str, bool]:
    """将任意值转换为可读的截断预览文本。

    Args:
        value: 待转换的任意值。
        max_chars: 截断长度上限（正整数）；None 表示不截断（全量返回）。

    Returns:
        (预览文本, 是否发生截断) 二元组。
    """
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
    if max_chars is None:
        return text, False
    truncated = len(text) > max_chars
    return text[:max_chars] + ("..." if truncated else ""), truncated


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
    截断阈值由 ``AuditLoggerConfig`` 声明，框架统一 model_validate 强转与校验。
    """

    config_cls = AuditLoggerConfig

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        self._call_count: dict[str, int] = {}
        # user_id -> 进行中 turn 状态
        self._turns: dict[str, _TurnState] = {}
        # user_id -> 已完成 turn span 列表（供测试断言）
        self._completed: dict[str, list[TurnSpan]] = {}

    def _arg_limit(self) -> int | None:
        """参数截断长度上限；preview_truncate 关闭时为 None（不截断）。"""
        return self.config.arg_max_chars if self.config.preview_truncate else None

    def _result_limit(self) -> int | None:
        """结果截断长度上限；preview_truncate 关闭时为 None（不截断）。"""
        return self.config.result_max_chars if self.config.preview_truncate else None

    def _user_limit(self) -> int | None:
        """用户输入原文截断长度上限；preview_truncate 关闭时为 None（不截断）。"""
        return self.config.user_max_chars if self.config.preview_truncate else None

    def _reply_limit(self) -> int | None:
        """回复预览截断长度上限；preview_truncate 关闭时为 None（不截断）。"""
        return self.config.reply_max_chars if self.config.preview_truncate else None

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
        arg_preview, arg_truncated = _preview(args, self._arg_limit())
        span_id = call_id or f"call_{state.counter + 1}"
        span = ToolSpan(
            span_id=span_id,
            tool_name=tool_name,
            start_time=_iso_now(),
            _pc_start=time.perf_counter(),
            arg_preview=arg_preview,
            arg_truncated=arg_truncated,
        )
        if not call_id:
            state.counter += 1
        state.span.tool_calls += 1
        state.pending.append(span)
        state.span.tool_spans.append(span)
        logger.debug(
            "[audit] tool-start user={} span={} tool={}",
            user_id, span.span_id, tool_name,
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
        span.close(result, self._result_limit())
        logger.info(
            "[audit] tool-end user={} span={} tool={} duration={:.1f}ms status={}",
            user_id, span.span_id, span.tool_name,
            span.duration_ms or 0.0, span.status,
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

        P2-1：主 JSONL 只落终态行 —— ``_write_turn`` 仅在本方法（终态
        Hook）中被调用，不存在中途 flush 写点。perf_counter 仅在进程内
        计算 duration_ms，不落盘。
        """
        user_id = _user_id(context)
        self._call_count[user_id] = self._call_count.get(user_id, 0) + 1

        state = self._turns.pop(user_id, None)
        if state is None:
            state = self._new_turn(user_id)
        span = state.span
        span.end_time = _iso_now()
        span.duration_ms = round(
            (time.perf_counter() - span._pc_start) * 1000, 3,
        )
        span.message_count = len(messages)
        span.iterations = _count_iterations(messages)
        span.tool_calls = _count_tool_calls(messages)
        span.finish_reasons = _infer_finish_reason(messages)

        # 内容侧字段（P1-1）：本轮用户输入原文 + 最终回复预览
        user_text, user_truncated = _extract_user_text(
            messages, self._user_limit(),
        )
        span.input_truncated = user_truncated
        # 存在 user 消息时记录输入消息（缺失时留空列表）
        if _has_role_content(messages, "user"):
            span.input_messages = [{"role": "user", "content": user_text}]

        reply_preview, reply_truncated = _extract_reply_preview(
            messages, self._reply_limit(),
        )
        span.output_truncated = reply_truncated
        # 存在 assistant 消息且 content 非空时记录输出消息
        if _has_role_content(messages, "assistant"):
            span.output_messages = [{"role": "assistant", "content": reply_preview}]

        # 估算 token 汇总（框架未把 usage 注入 messages，故用估算口径）
        span.input_tokens = _estimate_prompt_tokens(messages)
        span.output_tokens = _estimate_completion_tokens(messages)
        span.total_tokens = span.input_tokens + span.output_tokens

        # 未配对的 pending span 标记为 interrupted（守卫拦截或异常中断）
        pc_end = time.perf_counter()
        for pending_span in state.pending:
            pending_span.interrupted = True
            pending_span.end_time = span.end_time
            pending_span.duration_ms = round(
                (pc_end - pending_span._pc_start) * 1000, 3,
            )
            # interrupted 的 span status 落 "unset"（原设计落 error 语义不准）
            logger.warning(
                "[audit] tool-interrupted user={} span={} tool={}",
                user_id, pending_span.span_id, pending_span.tool_name,
            )

        self._completed.setdefault(user_id, []).append(span)
        self._write_turn(user_id, span)
        logger.info(
            "[audit] turn-end user={} round={} messages={} iterations={} "
            "tools={} input_tokens={} output_tokens={} finish_reasons={} "
            "duration={:.1f}ms",
            user_id, self._call_count[user_id], span.message_count,
            span.iterations, span.tool_calls, span.input_tokens,
            span.output_tokens, span.finish_reasons, span.duration_ms or 0.0,
        )

    # =========================================================================
    # 持久化
    # =========================================================================

    def _write_turn(self, user_id: str, span: TurnSpan) -> None:
        """将 turn span 写入 JSONL 文件，并输出结构化日志。

        asdict 直落：``span.to_contract_dict()`` 直接输出契约命名
        的 flat dict（字段名 = 契约键名），无需映射层。

        Args:
            user_id: 记录所属用户（用于 JSONL 文件名）。
            span: 待持久化的 turn span。
        """
        record = span.to_contract_dict()
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
                trace_id=f"turn_{uuid.uuid4().hex[:12]}",
                conversation_id=user_id or "default",
                agent_name=self.config.agent_name,
                start_time=_iso_now(),
                _pc_start=time.perf_counter(),
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
        """获取已完成 turn span 的 contract dict 列表，用于测试断言。

        Args:
            user_id: 过滤指定用户；None 返回全部用户的 span。

        Returns:
            turn span 的 contract dict 列表（含嵌套 tool span）。
        """
        if user_id is not None:
            return [s.to_contract_dict() for s in self._completed.get(user_id, [])]
        return [
            s.to_contract_dict()
            for spans in self._completed.values() for s in spans
        ]

    def tool_spans(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """获取已记录 tool span 的 contract dict 列表（含进行中与已完成的）。"""
        spans: list[ToolSpan] = []
        for u, state in self._turns.items():
            if user_id is None or u == user_id:
                spans.extend(state.span.tool_spans)
        for u, completed in self._completed.items():
            if user_id is None or u == user_id:
                for s in completed:
                    spans.extend(s.tool_spans)
        return [s.to_contract_dict() for s in spans]


# =============================================================================
# 辅助函数
# =============================================================================


def _user_id(context: Any) -> str:
    """从 context 提取 user_id，缺失时回退为 'default'。"""
    value = getattr(context, "user_id", None)
    return value if isinstance(value, str) and value else "default"


def _iso_now() -> str:
    """当前本地时区墙钟时间的 ISO 格式字符串。

    用于 start_time / end_time（OTel span 时间语义，墙钟）。
    perf_counter 仅在进程内用于 duration_ms 计算，不落盘。
    """
    return datetime.now().astimezone().isoformat()


def _last_content(messages: list[dict[str, Any]], role: str) -> Any:
    """取消息列表中指定角色的最后一条消息的 content。

    Args:
        messages: 本轮完整消息列表（全量历史 + 本轮输入）。
        role: 目标角色（"user" 或 "assistant"）。

    Returns:
        该角色最后一条消息的 content（任意类型）；不存在时返回 None。
    """
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == role:
            return m.get("content")
    return None


def _has_role_content(
    messages: list[dict[str, Any]], role: str,
) -> bool:
    """判断消息列表中是否存在指定角色的消息（content 存在或非空）。

    用于决定 input/output messages 是否需要在契约中构建消息条目：
    - ``_last_content`` 返回 None 表示该角色无消息 → 不构建条目。
    - content 为字符串时，空串（如 tool_calls 消息的 content=""）仍视为
      该角色有消息，但契约中 content 为空串是否保留由调用方决定。

    Args:
        messages: 消息列表。
        role: 目标角色。

    Returns:
        该角色存在消息时返回 True。
    """
    content = _last_content(messages, role)
    if content is None:
        return False
    # 非字符串 content（dict/list 等）视为有效内容
    if isinstance(content, str):
        return content != ""
    return True


def _extract_user_text(
    messages: list[dict[str, Any]],
    max_chars: int | None,
) -> tuple[str, bool]:
    """提取本轮用户输入原文（P1-1）。

    取最后一条 user 消息：result.messages 为全量历史 + 本轮输入，
    最后一条 user 消息即本轮输入（中轮注入消息亦为此语义）。
    已剥离 Runtime Context 注入段并折叠空白。

    Args:
        messages: 本轮完整消息列表。
        max_chars: 截断长度上限；None 表示不截断。

    Returns:
        (用户输入文本, 是否发生截断) 二元组。
    """
    content = _last_content(messages, "user")
    if content is None:
        return "", False
    if isinstance(content, str):
        content = strip_runtime_context(content)
    return _preview(content, max_chars)


def _extract_reply_preview(
    messages: list[dict[str, Any]],
    max_chars: int | None,
) -> tuple[str, bool]:
    """提取最终回复预览（P1-1，幻觉类案件的第一定位索引）。

    取最后一条 assistant 消息（若其仍携带 tool_calls，说明轮次以
    工具调用收尾，无最终回复文本，此时返回该消息 content 的预览）。

    Args:
        messages: 本轮完整消息列表。
        max_chars: 截断长度上限；None 表示不截断。

    Returns:
        (回复预览文本, 是否发生截断) 二元组。
    """
    content = _last_content(messages, "assistant")
    if content is None:
        return "", False
    return _preview(content, max_chars)


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


def _infer_finish_reason(messages: list[dict[str, Any]]) -> list[str]:
    """从消息列表推断结束原因（OTel 值域：stop / tool_calls）。

    框架未把 finish_reason 注入 messages（P1 约束），此处基于最后一条
    assistant 消息是否仍携带 tool_calls 做启发式推断。

    Args:
        messages: 本轮完整消息列表。

    Returns:
        ``["stop"]`` 或 ``["tool_calls"]``（OTel str[] 值域）。
    """
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            if m.get("tool_calls"):
                return ["tool_calls"]
            return ["stop"]
    return ["stop"]


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

    优先按 span_id（原生 call_id）精确匹配（框架透传的原生 ID 唯一）；
    call_id 为空时回退为按 tool_name 匹配最早的 span（兼容无 ID 注入的测试场景）。

    Args:
        pending: 进行中 turn 的待配对 span 列表。
        call_id: 待匹配的原生工具调用 ID。
        tool_name: 待匹配的工具名称（call_id 为空时的回退依据）。

    Returns:
        匹配到的 span；无匹配返回 None。
    """
    if call_id:
        for span in pending:
            if span.span_id == call_id:
                pending.remove(span)
                return span
        return None
    for span in pending:
        if span.tool_name == tool_name:
            pending.remove(span)
            return span
    return None
