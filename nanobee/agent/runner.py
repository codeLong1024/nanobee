"""Agent 执行引擎 - LLM 迭代循环与工具执行。

核心逻辑完全保留：LLM 调用循环、工具执行、上下文裁剪、注入处理、重试与恢复。
改造点：AgentRunSpec 新增 context_id 替代 session_key，移除废弃依赖。
沙箱传递：通过 ContextVar 注入，消除逐层参数透传。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from copy import deepcopy
from typing import Any

from nanobee.utils.logger import logger


from nanobee.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobee.agent.result_normalizer import ResultNormalizer
from nanobee.agent.specs import (
    AgentRunResult,
    AgentRunSpec,
    ExitReason,
    PluginHooks,
    _DEFAULT_ERROR_MESSAGE,
)
from nanobee.agent.tool_pipeline import ToolPipeline
from nanobee.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    map_finish_reason,
)
from nanobee.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
    StreamingFileEditTracker,
)
from nanobee.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    extract_tool_name,
    find_legal_message_start,
    strip_think,
)
from nanobee.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from nanobee.utils.notifications import get_notification_content
from nanobee.utils.runtime import (
    build_finalization_retry_message,
    build_length_recovery_message,
    is_blank_text,
    TRUNCATED_ARGS_ERROR_MESSAGE,
)

_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 2
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_SNIP_SAFETY_BUFFER = 1024
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"

# 向后兼容的模块属性，供测试/扩展 monkeypatch 使用
prepare_file_edit_tracker = _prepare_file_edit_tracker


class AgentRunner:
    """工具型 LLM 循环执行器，不含产品层逻辑。"""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self._tool_pipeline = ToolPipeline()
        self._result_normalizer = ResultNormalizer()

    @staticmethod
    def _classify_finish(finish_reason: str | None) -> bool:
        """provider 的 finish_reason → 是否失败。读归一值单点映射。

        ``map_finish_reason`` 把原始 finish_reason 折叠为四个语义档，
        此处把 error 与 blocked 档（refusal / content_filter）判定为失败，
        truncated 档由 length 恢复逻辑单独处理（PR-B 扩展）。

        Args:
            finish_reason: provider 返回的 finish_reason 原始串。

        Returns:
            True 表示该轮是 LLM 错误响应或内容被拦截。
        """
        return map_finish_reason(finish_reason) in ("error", "blocked")


    @staticmethod
    def _has_truncated_arguments(tool_call: ToolCallRequest) -> bool:
        """Return True if the tool call's raw arguments fail strict JSON parsing.

        Only meaningful when the response had a truncated (length) finish_reason.
        arguments_raw is None when the provider delivered arguments as a dict
        (no raw string to check); in that case the call is assumed complete.
        """
        raw = tool_call.arguments_raw
        if raw is None:
            return False
        try:
            json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return True
        return False

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """追加注入的用户消息，同时保持角色交替。"""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
    ) -> tuple[bool, int]:
        """排空待处理的注入消息。返回 (是否继续, 更新后的周期数)。"""
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        logger.info(
            "Injected {} follow-up message(s) {} ({}/{})",
            len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
        )
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """通过注入回调排空待处理的用户消息。"""
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """执行 Agent 迭代循环（LLM 调用 + 工具执行）。

        外层包裹 run-level hook（before_run / after_run / on_error / on_finally），
        迭代循环逻辑委托给 _run_core()。
        """
        _t_run = time.perf_counter()
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))

        try:
            await hook.before_run(context)
        except Exception:
            logger.exception("AgentHook.before_run failed")
        _t_before_run = time.perf_counter()

        try:
            logger.debug(
                "[RUNNER] 进入 _run_core (messages={}, before_run耗时={:.0f}ms)",
                len(messages), (_t_before_run - _t_run) * 1000,
            )
            result = await self._run_core(spec, hook, messages)
        except asyncio.CancelledError:
            context.messages = deepcopy(messages)
            context.exit_reason = ExitReason.CANCELLED
            context.exception = asyncio.CancelledError
            raise
        except Exception as exc:
            # 程序异常统一折叠进 error 返回，不 re-raise（CancelledError 已单独处理）。
            # run() 契约：要么正常返回（含 error 字段），要么 CancelledError。
            # 调用方（loop/subagent）无需再兜底异常，错误统一由 result.error 承载。
            context.messages = deepcopy(messages)
            context.exit_reason = ExitReason.COMPLETED
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            return AgentRunResult(
                final_content=None,
                messages=context.messages,
                tools_used=list(getattr(context, "tools_used", [])),
                usage={},
                exit_reason=ExitReason.COMPLETED,
                error=context.error,
                tool_events=[],
                had_injections=False,
            )
        else:
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.exit_reason = result.exit_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            try:
                await hook.on_finally(context)
            except Exception:
                logger.exception(
                    "AgentHook.on_finally error after %s",
                    context.exit_reason.value if context.exit_reason else "run exception",
                )

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        """迭代循环核心（LLM 调用 + 工具执行），由 run() 包裹。

        核心流程：上下文治理 → LLM 调用 → 工具执行 → 结果处理 → 循环/终止。
        """
        _t_core = time.perf_counter()
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        exit_reason = ExitReason.COMPLETED
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # 每轮对同一外部目标的重复尝试节流
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0

        for iteration in range(spec.max_iterations):
            try:
                _t_iter = time.perf_counter()
                # 保持持久化对话不变。上下文治理可能修复或压缩历史消息，
                # 但这些合成编辑不得改变调用者保存新轮次时使用的追加边界。
                _t_gov = time.perf_counter()
                messages_for_model = self._drop_orphan_tool_results(messages)
                _t1 = time.perf_counter()
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                _t2 = time.perf_counter()
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                _t3 = time.perf_counter()
                messages_for_model = self._snip_history(spec, messages_for_model)
                _t4 = time.perf_counter()
                # 裁剪可能产生新的孤立结果，清理它们。
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                _t5 = time.perf_counter()
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                _t6 = time.perf_counter()
                logger.debug(
                    "[GOVERNANCE] 第 {} 轮上下文治理: drop_orphan={:.0f}ms backfill={:.0f}ms "
                    "budget={:.0f}ms snip={:.0f}ms drop2={:.0f}ms backfill2={:.0f}ms total={:.0f}ms",
                    iteration,
                    (_t1 - _t_gov) * 1000, (_t2 - _t1) * 1000,
                    (_t3 - _t2) * 1000, (_t4 - _t3) * 1000,
                    (_t5 - _t4) * 1000, (_t6 - _t5) * 1000,
                    (_t6 - _t_gov) * 1000,
                )
            except Exception:
                logger.exception(
                    "Context governance failed on turn {} for {}; applying minimal repair",
                    iteration,
                    spec.context_id or "default",
                )
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(iteration=iteration, messages=messages)
            _t_hook = time.perf_counter()
            await hook.before_iteration(context)
            _elapsed_hook = (time.perf_counter() - _t_hook) * 1000
            if _elapsed_hook > 100:
                logger.debug("[GOVERNANCE] 第 {} 轮 before_iteration hook 耗时 {:.0f}ms", iteration, _elapsed_hook)

            # LLM API 调用计时：覆盖从 HTTP 请求发出到首 token 返回的全过程
            _t_llm_call = time.perf_counter()
            _elapsed_gov = (_t_llm_call - _t_core) * 1000
            logger.debug(
                "[LLM-CALL] 第 {} 轮 API 调用开始 (messages={}, tools={}, goverance耗时={:.0f}ms)",
                iteration, len(messages_for_model), len(spec.tools.get_definitions()), _elapsed_gov,
            )
            response = await self._request_model(spec, messages_for_model, hook, context)
            _elapsed_llm_call = (time.perf_counter() - _t_llm_call) * 1000
            logger.debug(
                "[LLM-CALL] 第 {} 轮 API 调用完成，耗时 {:.0f}ms (finish_reason={})",
                iteration, _elapsed_llm_call, response.finish_reason,
            )

            # finish_reason 字符串仅在循环边界映射一次为布尔语义，不向上泄漏。
            is_error = self._classify_finish(response.finish_reason)
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(usage, raw_usage)

            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            # [DEBUG] 打印 LLM 本轮输出摘要
            _content_preview = (response.content or "")[:120].replace("\n", "\\n")
            _tool_names = [tc.name for tc in (response.tool_calls or [])]
            logger.debug("[LLM] iter={} | content={} | tools={} | finish_reason={}",
                         iteration, _content_preview, _tool_names, response.finish_reason)

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                tools_used.extend(tc.name for tc in response.tool_calls)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._tool_pipeline.execute_all(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                )
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._result_normalizer.normalize(
                            result,
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            workspace=spec.workspace,
                            context_id=spec.context_id,
                            max_chars=spec.max_tool_result_chars,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    # 失败无"回复"，错误语义由 error 字段承载，不伪装成 assistant 回复
                    final_content = None
                    exit_reason = ExitReason.COMPLETED
                    context.final_content = final_content
                    context.error = error
                    context.exit_reason = exit_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                # 检查点 1：工具执行后、下次 LLM 调用前排空注入
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            # PR-B: truncated rounds with tool calls are dispatched individually below.
            if response.has_tool_calls and response.canonical_finish != "truncated":
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.context_id or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if response.canonical_finish == "normal" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.context_id or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.context_id or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                response = await self._request_finalization(spec, messages_for_model)
                retry_usage = self._usage_dict(response.usage)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.canonical_finish == "truncated" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.context_id or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    # PR-B: call-level dispatch for truncated rounds with tool calls.
                    # Valid calls execute normally; truncated calls receive an error
                    # tool result ("参数被截断未执行，请完整重发") instead of execution.
                    truncated_tool_names: list[str] = []
                    if response.has_tool_calls:
                        valid_calls = [
                            tc for tc in response.tool_calls
                            if not self._has_truncated_arguments(tc)
                        ]
                        truncated_calls = [
                            tc for tc in response.tool_calls
                            if self._has_truncated_arguments(tc)
                        ]
                        truncated_tool_names = [tc.name for tc in truncated_calls]

                        # Build assistant message including all tool calls
                        messages.append(build_assistant_message(
                            clean,
                            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                            reasoning_content=response.reasoning_content,
                            thinking_blocks=response.thinking_blocks,
                        ))
                        tools_used.extend(tc.name for tc in response.tool_calls)

                        # Execute valid calls
                        valid_results: list[Any] = []
                        if valid_calls:
                            valid_results, new_events, fatal_error = (
                                await self._tool_pipeline.execute_all(
                                    spec,
                                    valid_calls,
                                    external_lookup_counts,
                                    workspace_violation_counts,
                                )
                            )
                            tool_events.extend(new_events)
                            if fatal_error is not None:
                                error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                                final_content = None
                                exit_reason = ExitReason.COMPLETED
                                context.final_content = final_content
                                context.error = error
                                context.exit_reason = exit_reason
                                await hook.after_iteration(context)
                                break

                        # Append tool results for valid calls
                        for tc, result in zip(valid_calls, valid_results):
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": self._result_normalizer.normalize(
                                    result,
                                    tool_name=tc.name,
                                    tool_call_id=tc.id,
                                    workspace=spec.workspace,
                                    context_id=spec.context_id,
                                    max_chars=spec.max_tool_result_chars,
                                ),
                            })

                        # Synthesize error tool results for truncated calls
                        for tc in truncated_calls:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc.name,
                                "content": f"Error: {TRUNCATED_ARGS_ERROR_MESSAGE}",
                            })
                    else:
                        # No tool calls: keep original flow
                        messages.append(build_assistant_message(
                            clean,
                            reasoning_content=response.reasoning_content,
                            thinking_blocks=response.thinking_blocks,
                        ))

                    messages.append(build_length_recovery_message(
                        tool_names=truncated_tool_names or None,
                    ))
                    await hook.after_iteration(context)
                    continue

            # PR-B: truncated round with recovery exhausted — never deliver partial
            # truncated content as the final answer. Output a framework honest message
            # (not model output) and carry the truncation fact in context.error.
            if response.canonical_finish == "truncated" and not is_blank_text(clean) and not is_error:
                logger.warning(
                    "Output truncation exceeded {} recovery attempts for {}; emitting framework message",
                    _MAX_LENGTH_RECOVERIES,
                    spec.context_id or "default",
                )
                final_content = get_notification_content("turn_truncated")
                self._append_final_message(messages, final_content)
                error = (
                    f"Output truncated after {_MAX_LENGTH_RECOVERIES} recovery attempts. "
                    "The model repeatedly exceeded its output token limit."
                )
                exit_reason = ExitReason.COMPLETED
                context.final_content = None
                context.error = error
                context.exit_reason = exit_reason
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                await hook.after_iteration(context)
                break

            assistant_message: dict[str, Any] | None = None
            if not is_error and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # 在通知流结束前排空中轮注入。
            # 如果发现注入，保持流活跃 (resuming=True)，
            # 以免流式通道过早完成消息卡片。
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
            )
            if should_continue:
                had_injections = True

            # on_stream_end 是"流结束"传输信号，只表达 resuming，不夹带错误语义。
            # 错误语义唯一由 AgentRunResult.error 承载，经 loop 的系统通知（fail_card）下发。
            # 错误时流并未真正"结束"而是"中止"，跳过 on_stream_end，卡片停在 INPUTING，
            # 由 loop 的 fail_card 一步拉到 FAILED + 渲染错误文案，避免空 FINISHED 残留与二次终态化。
            if is_error:
                error = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
            elif hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if is_error:
                # 失败无"回复"，诊断信息不进对话历史
                final_content = None
                exit_reason = ExitReason.COMPLETED
                context.final_content = final_content
                context.error = error
                context.exit_reason = exit_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                error = "empty final response"
                # 失败无"回复"，诊断信息不进对话历史
                final_content = None
                exit_reason = ExitReason.COMPLETED
                context.final_content = final_content
                context.error = error
                context.exit_reason = exit_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.exit_reason = exit_reason
            await hook.after_iteration(context)
            break
        else:
            exit_reason = ExitReason.MAX_ITERATIONS
            if spec.max_iterations_message:
                final_content = spec.max_iterations_message.format(
                    max_iterations=spec.max_iterations,
                )
            else:
                final_content = get_notification_content(
                    "turn_max_iterations",
                    max_iterations=spec.max_iterations,
                )
            self._append_final_message(messages, final_content)
            # 排空剩余注入，追加到对话历史而非重新发布为独立入站消息。
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            exit_reason=exit_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ) -> LLMResponse:
        """调用 LLM 获取响应，支持流式/进度流式/普通三种模式。"""
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            raw = os.environ.get("NANOBEE_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        # 获取工具定义并应用过滤
        tool_definitions = spec.tools.get_definitions()
        if spec.filtered_tool_names is not None:
            allowed = set(spec.filtered_tool_names)
            tool_definitions = [
                d for d in tool_definitions
                if extract_tool_name(d) in allowed
            ]

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=tool_definitions,
        )
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        if (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        ):
            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(spec.progress_callback, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
                file_edit_tools=spec.file_edit_tools,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:
            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                if not delta:
                    return
                context.streamed_reasoning = True
                await hook.emit_reasoning(delta)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # 流式请求已有 provider 级空闲超时，不额外应用外层超时。
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def _request_finalization(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """空响应轮的重试：保留工具定义，允许模型继续调工具而非没收工具逼答。

        tools=None 的旧行为在真·空响应轮会把模型逼成编造文本 ——
        PR-A 将其废除：空响应恢复与普通轮走同一请求构造（含 spec.tools），
        由空重试门（canonical==normal）保证仅在正常终止轮进入。
        """
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(
            spec,
            retry_messages,
            tools=spec.tools.get_definitions(),
        )
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """丢弃历史中没有匹配 assistant tool_call 的工具结果。"""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """为孤立的 tool_use 块插入合成错误结果。"""
        declared: list[tuple[int, str, str]] = []
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """对每个工具结果应用长度预算限制。"""
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._result_normalizer.normalize(
                message.get("content"),
                tool_name=str(message.get("name") or "tool"),
                tool_call_id=str(message.get("tool_call_id") or f"tool_{idx}"),
                workspace=spec.workspace,
                context_id=spec.context_id,
                max_chars=spec.max_tool_result_chars,
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """当估算 token 超出预算时，从历史末尾裁剪消息。"""
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        _t0 = time.perf_counter()
        _tools_defs = spec.tools.get_definitions() if spec.tools else None
        _t1 = time.perf_counter()
        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            _tools_defs,
        )
        _t2 = time.perf_counter()
        logger.debug(
            "[SNIP-PERF] estimate_prompt_tokens_chain total={:.0f}ms "
            "(get_defs={:.0f}ms, encode={:.0f}ms) msgs={} tools={} est={} budget={}",
            (_t2 - _t0) * 1000,
            (_t1 - _t0) * 1000,
            (_t2 - _t1) * 1000,
            len(messages),
            len(_tools_defs) if _tools_defs else 0,
            estimate,
            budget,
        )

        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        remaining_budget = max(128, budget - system_tokens)
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept


