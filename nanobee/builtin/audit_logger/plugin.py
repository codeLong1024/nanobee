"""
audit_logger 参考插件 —— turn / tool 两级 span 审计

本插件是 TurnReport 结账单的**第一个消费者**，本身零观测逻辑：
框架（runner 账本 + loop 盖章）拥有 turn 真值，本插件只做
「TurnReport → 契约 dict」的纯映射。

- tool span：``on_pre_invoke`` 记录工具开始，``on_post_invoke`` 配对出工具
  完成 span（带原生 callId、耗时、status、参数摘要）——计时留在插件侧（策略）。
- turn span：``on_message_started`` 记录真实 turn 起点（含 turn 身份），
  ``on_message_completed`` 从 TurnReport 纯映射产出整轮 span（provider 实测
  token、逐轮 finish_reason 原值、迭代数、注入事实、退出原因、本轮输入/回复）。

**数据契约（v3）**：JSONL 输出的字段命名对齐 OTel GenAI Semantic Conventions：

- ``gen_ai.*``：严格采用 OTel GenAI 语义约定属性命名。
- ``nanobee.*``：框架自有概念（截断标记、注入事实、内部统计）。
- 无前缀通用字段（``schema``/``record_type``/``trace_id``/``start_time``/
  ``end_time`` 等）：通用 span 语义或契约元数据。

v3 语义变更（相对 v2，破坏性）：
- ``trace_id`` 从业务 ID ``turn_{uuid12}`` 变为框架 W3C trace id（32 hex），
  与日志流 ``set_trace_id`` 同源，可跨日志串联；兜底路径仍回退 uuid 格式。
- token 从字符估算变为 provider 实测（``nanobee.usage.estimated`` 恒 False）。
- ``gen_ai.response.finish_reasons`` 从启发式推断变为账本原值（保序去重）。
- ``gen_ai.input/output.messages`` 记录本轮**全部** user/assistant 消息
  （含 drain 注入的输入），修复注入后输入归因错位。
- 新增 ``nanobee.injections`` / ``nanobee.injected_messages`` /
  ``nanobee.exit_reason`` / ``nanobee.error``（runner 账本事实直通）。

一行 = 一个终态 turn（``record_type`` 字段恒为 ``turn``，为未来 span 树
中间层预留行判别）；turn 记录内嵌套 ``tool_spans`` 便于单行取全链路。
turn 记录统一经 ``_persist_span`` 落盘——未来往 span 树加中间层时只需
新增 span 类型并调用同一入口，落盘代码零改动。
turn 级时间三值（start_time / end_time / duration_ms）全部来自 loop 盖章
的 ``TurnReport``（turn_started_at / turn_ended_at），插件不持有第二时钟；
``perf_counter`` 单调时钟仅 tool span 计时在进程内使用，不落盘。
"""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from nanobee.agent.specs import IterationFact, TurnReport
from nanobee.exceptions import ContextError
from nanobee.plugins.base import NanobeePlugin
from nanobee.utils.helpers import strip_runtime_context
from nanobee.utils.observability import is_valid_trace_id
from nanobee.utils.redact import redact_secrets
from nanobee.utils.user_id import is_safe_user_id
from nanobee.utils.logger import logger

# 工具参数/结果的默认截断长度（AuditLoggerConfig 字段默认值）
_ARG_MAX_CHARS = 2000
_RESULT_MAX_CHARS = 2000
# turn 内容侧字段的默认截断长度（用户输入原文 / 最终回复预览）
_USER_MAX_CHARS = 500
_REPLY_MAX_CHARS = 800
# 失败诊断的默认截断长度（error 字段；截断前先做空白折叠，防 JSONL 断行）
_ERROR_MAX_CHARS = 500

# 失败诊断脱敏与错误串归一化统一由 nanobee.utils.redact 提供（单一处理点）。
# 同一 error 串还会流向用户可见的通知 content 与 metadata.error_detail，
# 若在本插件再持一份私有正则，两侧规则必然漂移（2026-09-13 评审缺陷：
# 审计侧已脱敏而用户可见侧裸串透传）。本插件只负责在落盘前调用。

# 判定工具结果是否为错误的标志（工具返回字符串时据此推断 isError）
_ERROR_MARKERS = ("error:", "exception:", "failed", "错误", "异常", "失败")

# OTel GenAI operation name 固定值
_TURN_OPERATION = "invoke_agent"
_TOOL_OPERATION = "execute_tool"

# _completed 环形队列上界（仅测试断言辅助，非观测数据；防长驻实例无界增长）
_COMPLETED_HISTORY_MAX = 64

# 契约版本标识（v3：token 实测 + 注入事实 + W3C trace id，替代 v2 估算口径）
_SCHEMA = "nanobee.audit/3"


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
        error_max_chars: turn 记录失败诊断截断长度上限（正整数）。
    """

    agent_name: str = "nanobee"
    preview_truncate: bool = True
    arg_max_chars: int = Field(default=_ARG_MAX_CHARS, ge=1)
    result_max_chars: int = Field(default=_RESULT_MAX_CHARS, ge=1)
    user_max_chars: int = Field(default=_USER_MAX_CHARS, ge=1)
    reply_max_chars: int = Field(default=_REPLY_MAX_CHARS, ge=1)
    error_max_chars: int = Field(default=_ERROR_MAX_CHARS, ge=1)


@dataclass
class ToolSpan:
    """单个工具调用的 span 记录（OTel GenAI 契约命名）。

    ``span_id`` 为框架透传的原生工具调用 ID（ToolCallRequest.id），空则
    回退生成；用于在同一 turn 内将 on_pre_invoke / on_post_invoke 精确配对。
    ``_pc_start`` 为进程内 perf_counter 起点，仅用于计算 duration_ms，不落盘。
    """

    record_type: ClassVar[str] = "tool"    # 契约记录类型（JSONL 行判别）
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
            "schema": _SCHEMA,
            "record_type": self.record_type,
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

    turn = 一条 trace：``trace_id`` 为框架提供的 W3C trace id（32 hex），
    与日志流 ``set_trace_id`` 同源；兜底路径（插件中途启用）回退
    ``turn_{uuid12}``。消息结构简化：``input_messages`` / ``output_messages``
    以 ``[{"role", "content"}]`` 简化格式存储（OTel 完整 parts 结构由
    bridge 组装）。时间三值由 loop 盖章的 ``TurnReport`` 承载，
    插件不持有第二时钟。
    """

    record_type: ClassVar[str] = "turn"    # 契约记录类型（JSONL 行判别）
    trace_id: str = ""                     # W3C trace id（兜底 turn_{uuid12}）
    agent_name: str = "nanobee"            # gen_ai.agent.name（配置项）
    conversation_id: str = "default"       # gen_ai.conversation.id
    start_time: str = ""                   # ISO 墙钟（was ts_start_iso）
    end_time: str = ""                     # ISO 墙钟（was ts_end_iso）
    duration_ms: float | None = None
    input_tokens: int = 0                  # gen_ai.usage.input_tokens（provider 实测）
    output_tokens: int = 0                 # gen_ai.usage.output_tokens（provider 实测）
    total_tokens: int = 0                  # gen_ai.usage.total_tokens
    finish_reasons: list[str] = field(default_factory=list)
    # gen_ai.response.finish_reasons：账本原值，保序去重
    input_messages: list[dict] = field(default_factory=list)
    # gen_ai.input.messages: [{role, content}]（本轮全部 user 消息，含注入）
    output_messages: list[dict] = field(default_factory=list)
    # gen_ai.output.messages: [{role, content}]（本轮全部 assistant 文本回复）
    input_truncated: bool = False          # nanobee.input.truncated
    output_truncated: bool = False         # nanobee.output.truncated
    iterations: int = 0                    # nanobee.iterations（账本事实）
    message_count: int = 0                 # nanobee.messages（本轮窗口消息数）
    tool_calls: int = 0                    # nanobee.tool_calls（hook 累计真值）
    injections: int = 0                    # nanobee.injections（排空注入次数）
    injected_messages: int = 0             # nanobee.injected_messages（注入消息总条数）
    exit_reason: str = ""                  # nanobee.exit_reason（runner 出口盖章）
    error: str | None = None               # nanobee.error（runner 失败权威字段）
    tool_spans: list[ToolSpan] = field(default_factory=list)

    def to_contract_dict(self) -> dict[str, Any]:
        """序列化为 OTel GenAI 契约命名的 flat dict（含嵌套 tool_spans）。"""
        return {
            "schema": _SCHEMA,
            "record_type": self.record_type,
            "trace_id": self.trace_id,
            "gen_ai.operation.name": _TURN_OPERATION,
            "gen_ai.agent.name": self.agent_name,
            "gen_ai.conversation.id": self.conversation_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "gen_ai.usage.input_tokens": self.input_tokens,
            "gen_ai.usage.output_tokens": self.output_tokens,
            "gen_ai.usage.total_tokens": self.total_tokens,
            "nanobee.usage.estimated": False,
            "gen_ai.response.finish_reasons": self.finish_reasons,
            "gen_ai.input.messages": self.input_messages,
            "gen_ai.output.messages": self.output_messages,
            "nanobee.input.truncated": self.input_truncated,
            "nanobee.output.truncated": self.output_truncated,
            "nanobee.iterations": self.iterations,
            "nanobee.messages": self.message_count,
            "nanobee.tool_calls": self.tool_calls,
            "nanobee.injections": self.injections,
            "nanobee.injected_messages": self.injected_messages,
            "nanobee.exit_reason": self.exit_reason,
            "nanobee.error": self.error,
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

    实现 ``on_message_started`` / ``on_pre_invoke`` / ``on_post_invoke`` /
    ``on_message_completed`` 四个 Hook，零贡献提示词与工具。
    completed 为 TurnReport → 契约的纯映射；截断阈值由 ``AuditLoggerConfig``
    声明，框架统一 model_validate 强转与校验。
    """

    config_cls = AuditLoggerConfig

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        self._call_count: dict[str, int] = {}
        # user_id -> 进行中 turn 状态
        self._turns: dict[str, _TurnState] = {}
        # user_id -> 已完成 turn span 环形队列（仅供测试断言，有界防长驻实例
        # 内存无界增长；_COMPLETED_HISTORY_MAX 覆盖单用户连续断言场景）
        self._completed: dict[str, deque[TurnSpan]] = {}

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

    def _error_limit(self) -> int | None:
        """失败诊断截断长度上限；preview_truncate 关闭时为 None（不截断）。"""
        return self.config.error_max_chars if self.config.preview_truncate else None

    # =========================================================================
    # 整轮 span 起点（on_message_started）
    # =========================================================================

    async def on_message_started(
        self,
        context: Any,
        message: str,
        turn_id: str,
    ) -> None:
        """记录本轮 turn 的真实起点与身份。

        由框架在 turn 开始时触发，替代旧的「首个工具调用 / 完成时」lazy
        创建——修复纯聊天（零工具调用）turn 的 duration_ms ≈ 0 失真。
        ``turn_id``（W3C trace id）写入 span 的 trace_id，与日志流同源。
        若发现未完结的旧 turn 状态（completed Hook 未触发），丢弃陈旧
        状态避免新 turn 被并入旧 span；正常路径下事件循环 FIFO 保证
        completed 先于下一轮 started 执行。

        Args:
            context: 当前用户上下文(UserContext 实例)
            message: 用户原始输入文本
            turn_id: turn 唯一标识（与 TurnReport.turn_id 同源）
        """
        user_id = _user_id(context)
        if user_id in self._turns:
            logger.warning(
                "[audit] turn-start 发现未完结的旧 turn，丢弃 user={}", user_id,
            )
        self._turns[user_id] = self._new_turn(user_id, trace_id=turn_id)
        logger.debug("[audit] turn-start user={} turn={}", user_id, turn_id)

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
            # 兜底：on_message_started 未触发的场景（插件中途启用等）
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
    # 整轮 span（on_message_completed）—— TurnReport 纯映射
    # =========================================================================

    async def on_message_completed(
        self,
        context: Any,
        report: TurnReport,
    ) -> None:
        """从 TurnReport 纯映射产出 turn span 并持久化。

        本方法是「结账单消费者」：所有统计字段均来自 runner 账本与
        loop 盖章，禁止从消息历史启发式反推（v2 的教训——全量历史污染
        计数、drain 注入归因错位、token 字符估算失真）。

        Args:
            context: 当前用户上下文(UserContext 实例)。
            report: turn 结账单（真值唯一来源）。
        """
        user_id = _user_id(context)
        self._call_count[user_id] = self._call_count.get(user_id, 0) + 1

        state = self._turns.get(user_id)
        if state is not None and self._is_foreign_turn_state(state, report):
            # 所有权校验（评审 F12）：单槽里是更新一轮的状态（其 started 已覆盖）。
            # 不 pop（留给属于它的 completed）也不覆盖；本次 report 用独立
            # 兜底状态落盘，防跨 turn 归并与 trace_id 错配。
            state = self._detached_turn(user_id, report.turn_id, report.turn_started_at)
        elif state is not None:
            self._turns.pop(user_id, None)
        else:
            # 兜底：on_message_started 未触发的场景（插件中途启用等）
            state = self._detached_turn(user_id, report.turn_id, report.turn_started_at)
        span = state.span
        ledger = report.ledger
        window = report.messages_window

        # 身份与时间：turn 级时间三值全部来自 loop 盖章（同源同钟），
        # 插件不持有第二时钟；turn_ended_at 缺失时 end 退化为当前墙钟（兜底路径）。
        if report.turn_id:
            span.trace_id = report.turn_id
        if report.turn_started_at:
            span.start_time = report.turn_started_at
        span.end_time = report.turn_ended_at or _iso_now()
        span.duration_ms = _iso_diff_ms(span.start_time, span.end_time)

        # 迭代事实（账本直通，纯映射）
        span.iterations = len(ledger.iterations)
        span.finish_reasons = _dedup_finish_reasons(ledger.iterations)
        span.input_tokens = sum(
            f.usage.get("prompt_tokens", 0) for f in ledger.iterations
        )
        span.output_tokens = sum(
            f.usage.get("completion_tokens", 0) for f in ledger.iterations
        )
        span.total_tokens = span.input_tokens + span.output_tokens

        # 本轮窗口事实：消息数 / 注入 / 退出原因 / 失败诊断
        span.message_count = len(window)
        span.injections = len(ledger.injections)
        span.injected_messages = sum(f.count for f in ledger.injections)
        span.exit_reason = ledger.exit_reason
        # 失败诊断：**先脱敏再截断**——第三方异常文案不可控（URL 鉴权密钥
        # 可经 ledger.error 进入审计文件），截断边界可能切断键值对，故
        # 掩码必须发生在 500 字符截断之前；空白折叠顺带消除 U+2028 等
        # 行分隔符对 JSONL 的断行。同一字符串经 _persist_span 共用于
        # 文件与 [audit-json] 日志，两处同源脱敏。
        if ledger.error is not None:
            redacted = redact_secrets(ledger.error)
            span.error = _preview(redacted, self._error_limit())[0]
        else:
            span.error = None

        # 工具调用计数：hook 累计真值（pre_invoke 逐次累加），不再被历史重算覆盖。
        # 与嵌套 tool_spans 一一对应（守卫拦截的 interrupted span 也计入）。
        span.tool_calls = len(span.tool_spans)

        # 内容侧字段：本轮窗口内**全部** user 输入（含注入）与 assistant 文本回复。
        # 修复 v2「只取最后一条」导致的 drain 输入归因错位与中间回复丢失。
        span.input_messages = []
        span.input_truncated = False
        span.output_messages = []
        span.output_truncated = False
        for m in window:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if content is None:
                continue
            if role == "user":
                preview, truncated = _content_preview(content, self._user_limit())
                span.input_messages.append({"role": "user", "content": preview})
                span.input_truncated = span.input_truncated or truncated
            elif role == "assistant":
                # 空文本（tool_calls 收尾消息）不进回复列表，由 tool span 覆盖
                if isinstance(content, str) and content == "":
                    continue
                preview, truncated = _content_preview(content, self._reply_limit())
                span.output_messages.append({"role": "assistant", "content": preview})
                span.output_truncated = span.output_truncated or truncated

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

        self._completed.setdefault(
            user_id, deque(maxlen=_COMPLETED_HISTORY_MAX),
        ).append(span)
        self._persist_span(user_id, span)
        logger.info(
            "[audit] turn-end user={} round={} messages={} iterations={} "
            "tools={} input_tokens={} output_tokens={} finish_reasons={} "
            "injections={} duration={:.1f}ms",
            user_id, self._call_count[user_id], span.message_count,
            span.iterations, span.tool_calls, span.input_tokens,
            span.output_tokens, span.finish_reasons, span.injections,
            span.duration_ms or 0.0,
        )

    # =========================================================================
    # 持久化
    # =========================================================================

    def _persist_span(self, user_id: str, span: TurnSpan | ToolSpan) -> None:
        """唯一的 span 落盘入口。

        ``span.to_contract_dict()`` 直接输出契约命名的 flat dict
        （含 ``record_type`` 行判别字段），序列化一次、文件写与
        ``[audit-json]`` 结构化日志共用同一字符串。未来往 span 树加
        中间层时，只需为新增 span 类型实现 ``to_contract_dict`` 并调用
        本方法，落盘代码零改动。

        Args:
            user_id: 记录所属用户（用于 JSONL 文件名）。
            span: 待持久化的 span（TurnSpan 或 ToolSpan）。
        """
        line = json.dumps(span.to_contract_dict(), ensure_ascii=False)
        self._write_jsonl(user_id, line)

        # 结构化日志（单行 JSON，与文件内容一致）
        logger.info("[audit-json] {}", line)

    def _jsonl_path(self, user_id: str) -> Path:
        """解析 JSONL 输出路径。

        优先 ``<context_root>/audit_logger/<user_id>.jsonl``；
        context_root 未注入时回退到系统临时目录下的进程级文件，
        保证测试与无注入场景不写坏工作区。

        Args:
            user_id: 当前记录所属用户。存储键安全由**双层防线**保证
                （评审 F1 落点断言）：出生点归一（``InboundMessage.context_id``
                属性经 ``resolve_storage_key``）+ 本方法拼路径前断言，
                非法值拒绝落盘而非写错位置。

        Returns:
            JSONL 文件的绝对路径。

        Raises:
            ContextError: user_id 未通过存储键白名单校验。
        """
        if not is_safe_user_id(user_id or "default"):
            raise ContextError(f"audit JSONL 拒绝非法 user_id: {user_id!r}")
        root = self.context_root
        base = Path(root) if root else Path(tempfile.gettempdir()) / "nanobee-audit"
        return base / "audit_logger" / f"{user_id or 'default'}.jsonl"

    def _write_jsonl(self, user_id: str, line: str) -> None:
        """以追加模式将一行 JSON 写入 JSONL。

        Args:
            user_id: 当前记录所属用户。
            line: 已序列化的单行 JSON（不含换行符）。
        """
        path = self._jsonl_path(user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.exception("[audit] 写入 JSONL 失败: {}", path)

    # =========================================================================
    # 内部状态辅助
    # =========================================================================

    def _new_turn(self, user_id: str, trace_id: str = "") -> _TurnState:
        """建立新 turn 状态并登记进单槽；``trace_id`` 缺省回退 uuid。"""
        state = _TurnState(
            span=TurnSpan(
                trace_id=trace_id or f"turn_{uuid.uuid4().hex[:12]}",
                conversation_id=user_id or "default",
                agent_name=self.config.agent_name,
                start_time=_iso_now(),
            ),
        )
        self._turns[user_id] = state
        return state

    @staticmethod
    def _is_foreign_turn_state(state: _TurnState, report: TurnReport) -> bool:
        """判断单槽中的状态是否属于「另一轮」（评审 F12 所有权校验）。

        仅当单槽状态携带 W3C turn id 且与 ``report.turn_id`` 不同时视为
        外来（started(N+1) 已覆盖单槽的竞态窗口）；``turn_{uuid}`` 兜底
        格式来自**本 turn** 的 pre_invoke 兜底（started 未触发的场景），
        应被本次 report 认领，不算外来。

        Args:
            state: 单槽中的现有 turn 状态。
            report: 本次结账单。

        Returns:
            True 表示单槽状态属于另一轮，本次 report 不得动它。
        """
        if not report.turn_id:
            return False
        existing = state.span.trace_id
        return is_valid_trace_id(existing) and existing != report.turn_id

    def _detached_turn(
        self, user_id: str, trace_id: str, started_at: str,
    ) -> _TurnState:
        """构造不登记进单槽的独立 turn 状态（completed 兜底 / 所有权错配用）。

        Args:
            user_id: 记录所属用户。
            trace_id: 框架 turn 身份；空则回退 uuid 兜底格式。
            started_at: loop 盖章的 dispatch 墙钟；空则退化当前墙钟。
        """
        return _TurnState(
            span=TurnSpan(
                trace_id=trace_id or f"turn_{uuid.uuid4().hex[:12]}",
                conversation_id=user_id or "default",
                agent_name=self.config.agent_name,
                start_time=started_at or _iso_now(),
            ),
        )

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


def _iso_diff_ms(start: str, end: str) -> float | None:
    """计算两个 ISO 墙钟时间差（毫秒）。

    墙钟差受 NTP 校时影响（评审 D3 拍板接受：审计秒级精度足够），
    换取消灭插件侧第二时钟，保证 start + duration == end 自洽。

    Args:
        start: 起始 ISO 时间字符串。
        end: 结束 ISO 时间字符串。

    Returns:
        毫秒差（round 3）；任一侧解析失败返回 None。
    """
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return round(delta.total_seconds() * 1000, 3)


def _content_preview(content: Any, max_chars: int | None) -> tuple[str, bool]:
    """消息 content → 预览文本（字符串剥离 Runtime Context，其余 JSON 化）。"""
    if isinstance(content, str):
        content = strip_runtime_context(content)
    return _preview(content, max_chars)


def _dedup_finish_reasons(iterations: list[IterationFact]) -> list[str]:
    """从账本迭代事实提取 finish_reason 原值（保序去重）。

    Args:
        iterations: 账本迭代事实列表。

    Returns:
        原值列表（保序去重）；无迭代时为空列表。
    """
    seen: list[str] = []
    for fact in iterations:
        reason = fact.finish_reason
        if reason and reason not in seen:
            seen.append(reason)
    return seen


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
