"""工具执行流水线 —— 守卫链 → 执行 → 错误分类。

核心模式：ToolPipeline.execute_one() 是守卫链模式——
每个守卫检查一个前置条件，不满足时直接返回错误，通过后才进入下一守卫。
最终到达核心执行阶段，统一处理异常和错误结果。
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobee.exceptions import SandboxViolationError
from nanobee.kernel.context_sandbox_var import current_sandbox
from nanobee.providers.base import ToolCallRequest
from nanobee.utils.file_edit_events import (
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
    StreamingFileEditTracker,
)
from nanobee.utils.logger import logger
from nanobee.utils.progress_events import (
    build_tool_event_start_payload,
    invoke_file_edit_progress,
    invoke_on_progress,
    on_progress_accepts_file_edit_events,
)
from nanobee.utils.runtime import repeated_external_lookup_error

from nanobee.agent.fault_classifier import FaultClassifier
from nanobee.agent.specs import AgentRunSpec
from nanobee.utils.constants import _HINT

# 工具执行返回：三元组 (结果, 事件字典, 致命错误)
ToolResult = tuple[Any, dict[str, str], BaseException | None]


class ToolPipeline:
    """工具执行流水线 —— 守卫链 + 核心执行 + 错误分类。

    execute_one() 是入口，内部按顺序执行：
    1. 外部查询节流检查
    2. 工具实例化（prepare_call）
    3. 文件编辑追踪初始化
    4. 工具名过滤检查
    5. 沙箱参数清洗
    6. 通道上下文注入
    7. Plugin pre-invoke hooks
    8. 进度通知
    9. 核心执行（tool.execute）
    10. 异常/错误统一处理（委托 FaultClassifier）

    使用方式：
        pipeline = ToolPipeline()
        result, event, error = await pipeline.execute_one(
            spec, tool_call, ext_lookup_counts, ws_violation_counts,
        )
    """

    def __init__(self) -> None:
        self._fault_classifier = FaultClassifier()

    # =========================================================================
    # 公开入口
    # =========================================================================

    async def execute_all(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        """执行一批工具调用，含批次分组和并发控制。

        Args:
            spec: Agent 执行配置。
            tool_calls: 待执行的工具调用列表。
            external_lookup_counts: 外部查询节流计数（跨迭代累积）。
            workspace_violation_counts: 工作区违规计数（跨迭代累积）。

        Returns:
            (results, events, fatal_error) — 首个致命错误会中断返回。
        """
        batches = self._partition(spec, tool_calls)
        tool_results: list[ToolResult] = []

        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    self.execute_one(
                        spec, tc, external_lookup_counts, workspace_violation_counts,
                    )
                    for tc in batch
                ))
                tool_results.extend(batch_results)
            else:
                for tc in batch:
                    result = await self.execute_one(
                        spec, tc, external_lookup_counts, workspace_violation_counts,
                    )
                    tool_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def execute_one(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> ToolResult:
        """执行单个工具调用（守卫链模式）。

        每个守卫返回 ToolResult 则短路，返回 None 则继续。
        """
        # 守卫 1：外部查询节流
        if intercepted := self._guard_throttle(spec, tool_call, external_lookup_counts):
            return intercepted

        # 日志：工具请求
        args_str = str(tool_call.arguments)
        if len(args_str) > 500:
            args_str = args_str[:500] + "...(truncated)"
        logger.info("[TOOL] 请求: {} | args={}", tool_call.name, args_str)

        # 工具预备：获取工具实例和参数
        tool, params, prep_error = self._prepare(spec, tool_call)
        if prep_error:
            return self._handle_prep_error(prep_error, spec, tool_call, workspace_violation_counts)

        # 文件编辑追踪上下文（横切关注点）
        fe_tracker = self._setup_file_edit_tracker(spec, tool_call, tool, params)
        await self._emit_file_edit_start(fe_tracker, params)

        # 守卫 2：工具名过滤
        if intercepted := self._guard_filter(spec, tool_call):
            return intercepted

        # 守卫 3：沙箱参数清洗（传入 tool 以读取 x-constraint 声明）
        params, sandbox_error = self._guard_sandbox(tool_call, params, tool)
        if sandbox_error:
            result_str = str(sandbox_error)
            if len(result_str) > 200:
                result_str = result_str[:200] + "..."
            logger.info("[TOOL] 结果: {} = 沙箱拦截: {}", tool_call.name, result_str)
            return sandbox_error + _HINT, self._error_event(tool_call.name, f"sandbox: {sandbox_error}"), None

        # 守卫 4：Plugin pre-invoke hooks
        params, hook_error = await self._guard_pre_hooks(spec, tool_call, params)
        if hook_error:
            return hook_error + _HINT, self._error_event(tool_call.name, f"plugin-hook: {hook_error}"), None

        # 通知通道：工具开始执行
        await self._notify_tool_start(spec, tool_call)

        # 核心执行 + 统一错误处理
        return await self._execute_core_and_handle(
            spec, tool_call, tool, params, fe_tracker, workspace_violation_counts,
        )

    # =========================================================================
    # 守卫方法
    # =========================================================================

    @staticmethod
    def _guard_throttle(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
    ) -> ToolResult | None:
        """检查外部查询节流：对同一目标重复请求时返回错误。"""
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
            spec.throttled_tool_names,
        )
        if not lookup_error:
            return None
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": "repeated external lookup blocked",
        }
        if spec.fail_on_tool_error:
            return lookup_error + _HINT, event, RuntimeError(lookup_error)
        return lookup_error + _HINT, event, None

    @staticmethod
    def _guard_filter(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
    ) -> ToolResult | None:
        """检查工具是否在允许列表中。"""
        if spec.filtered_tool_names is None:
            return None
        if tool_call.name in spec.filtered_tool_names:
            return None
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": f"tool not found: {tool_call.name}",
        }
        msg = (
            f"Error: Tool '{tool_call.name}' not found. "
            f"Available: {', '.join(spec.filtered_tool_names)}"
        ) + _HINT
        return msg, event, None

    @staticmethod
    def _guard_sandbox(
        tool_call: ToolCallRequest,
        params: Any,
        tool: Any = None,
    ) -> tuple[Any, str | None]:
        """通过 ContextVar 获取沙箱，读取工具声明的 x-constraint 清洗路径参数。

        工具通过 JSON Schema 的 x-constraint 属性声明各参数需要的约束类型，
        框架只读声明、执行对应约束，不猜测参数语义。

        Returns:
            (清洗后参数, 错误消息或 None)
        """
        request_sandbox = current_sandbox()
        if request_sandbox is None or not isinstance(params, dict):
            return params, None
        # 从工具声明中提取参数约束信息
        param_schema = None
        if tool is not None:
            param_schema = getattr(tool, "parameters", None)
        try:
            params = request_sandbox.sanitize_params(
                tool_call.name, params, param_schema=param_schema,
            )
            return params, None
        except (PermissionError, SandboxViolationError) as e:
            return params, str(e)

    @staticmethod
    async def _guard_pre_hooks(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """执行 Plugin pre-invoke hooks。返回 (修改后参数, 错误消息或 None)。"""
        if not spec.plugin_hooks or not isinstance(params, dict):
            return params, None
        for hook_fn in spec.plugin_hooks.get("pre_invoke", []):
            try:
                params = await hook_fn(tool_call.name, params)
            except (PermissionError, SandboxViolationError) as e:
                return params, str(e)
            except Exception as e:
                logger.exception("on_pre_invoke hook 执行出错: {}", e)
                # 不阻止工具执行
        return params, None

    # =========================================================================
    # 核心执行
    # =========================================================================

    async def _execute_core_and_handle(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
        fe_tracker: _FileEditContext | None,
        workspace_violation_counts: dict[str, int],
    ) -> ToolResult:
        """执行工具并在异常/错误时委托 FaultClassifier 处理。"""
        try:
            # 实际执行
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)

            # Plugin post-invoke hooks
            result = await self._apply_post_hooks(spec, tool_call, result)

            # 日志：工具结果
            result_str = str(result)
            if len(result_str) > 300:
                result_str = result_str[:300] + "...(truncated)"
            logger.info("[TOOL] 结果: {} = {}", tool_call.name, result_str)

        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            await self._emit_file_edit_error(fe_tracker, str(exc))
            return self._classify_exception(exc, spec, tool_call, workspace_violation_counts)

        # 成功路径
        await self._emit_file_edit_end(fe_tracker, params)
        return result, self._success_event(tool_call.name, result), None

    # =========================================================================
    # 错误分类（委托 FaultClassifier）
    # =========================================================================

    def _classify_exception(
        self,
        exc: Exception,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> ToolResult:
        """分类执行时异常，委托 FaultClassifier。"""
        detail = str(exc).replace("\n", " ").strip()[:120]
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": detail,
        }
        payload = f"Error: {type(exc).__name__}: {exc}"
        soft = payload + _HINT
        handled = self._fault_classifier.classify(
            raw_text=str(exc),
            soft_payload=soft,
            tool_call=tool_call,
            workspace_violation_counts=workspace_violation_counts,
            exception=exc,
            exec_capable_tools=spec.exec_capable_tools,
        )
        if handled is not None:
            return handled
        if spec.fail_on_tool_error:
            return soft, event, exc
        return soft, event, None

    # =========================================================================
    # 工具预备
    # =========================================================================

    @staticmethod
    def _prepare(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
    ) -> tuple[Any, dict[str, Any], str | None]:
        """预备工具调用：通过 prepare_call 获取工具实例和参数。

        Returns:
            (tool, params, prep_error) — prep_error 非 None 表示预备失败。
        """
        prepare_call = getattr(spec.tools, "prepare_call", None)
        if not callable(prepare_call):
            return None, tool_call.arguments, None
        try:
            prepared = prepare_call(tool_call.name, tool_call.arguments)
            if isinstance(prepared, tuple) and len(prepared) == 3:
                return prepared
        except Exception:
            logger.debug("prepare_call 失败，使用原始参数")
        return None, tool_call.arguments, None

    def _handle_prep_error(
        self,
        prep_error: str,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> ToolResult:
        """处理工具预备阶段的错误。先尝试 FaultClassifier 分类，再回退。"""
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": prep_error.split(": ", 1)[-1][:120],
        }
        handled = self._fault_classifier.classify(
            raw_text=prep_error,
            soft_payload=prep_error + _HINT,
            tool_call=tool_call,
            workspace_violation_counts=workspace_violation_counts,
            exec_capable_tools=spec.exec_capable_tools,
        )
        if handled is not None:
            return handled
        return prep_error + _HINT, event, (
            RuntimeError(prep_error) if spec.fail_on_tool_error else None
        )

    # =========================================================================
    # Plugin post-invoke hooks
    # =========================================================================

    @staticmethod
    async def _apply_post_hooks(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        result: Any,
    ) -> Any:
        """执行 Plugin post-invoke hooks。"""
        if not spec.plugin_hooks:
            return result
        for hook_fn in spec.plugin_hooks.get("post_invoke", []):
            try:
                result = await hook_fn(tool_call.name, result)
            except Exception as e:
                logger.exception("on_post_invoke hook 执行出错: {}", e)
        return result

    # =========================================================================
    # 文件编辑追踪（横切关注点）
    # =========================================================================

    @staticmethod
    def _setup_file_edit_tracker(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        tool: Any,
        params: Any,
    ) -> "_FileEditContext | None":
        """设置文件编辑追踪器，若通道不支持则返回 None。"""
        if spec.progress_callback is None:
            return None
        if not on_progress_accepts_file_edit_events(spec.progress_callback):
            return None
        trackers = prepare_file_edit_trackers(
            call_id=tool_call.id,
            tool_name=tool_call.name,
            tool=tool,
            workspace=spec.workspace,
            params=params if isinstance(params, dict) else None,
            file_edit_tools=spec.file_edit_tools,
        )
        if not trackers:
            return None
        return _FileEditContext(trackers, spec.progress_callback)

    @staticmethod
    async def _emit_file_edit_start(fe_tracker: "_FileEditContext | None", params: Any) -> None:
        if fe_tracker:
            await fe_tracker.emit_start(params)

    @staticmethod
    async def _emit_file_edit_end(fe_tracker: "_FileEditContext | None", params: Any) -> None:
        if fe_tracker:
            await fe_tracker.emit_end(params)

    @staticmethod
    async def _emit_file_edit_error(fe_tracker: "_FileEditContext | None", error: str) -> None:
        if fe_tracker:
            await fe_tracker.emit_error(error)

    @staticmethod
    async def _notify_tool_start(spec: AgentRunSpec, tool_call: ToolCallRequest) -> None:
        """通知通道工具开始执行（触发视觉反馈）。"""
        # message 是 LLM 输出投递载体，非真正工具，无需通知通道触发视觉反馈
        if tool_call.name == "message":
            return
        if spec.progress_callback is not None:
            await invoke_on_progress(
                spec.progress_callback, "",
                tool_hint=True,
                tool_events=[build_tool_event_start_payload(tool_call)],
            )

    # =========================================================================
    # 事件构建
    # =========================================================================

    @staticmethod
    def _error_event(name: str, detail: str) -> dict[str, str]:
        return {"name": name, "status": "error", "detail": detail}

    @staticmethod
    def _success_event(name: str, result: Any) -> dict[str, str]:
        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return {"name": name, "status": "ok", "detail": detail}

    # =========================================================================
    # 批次分组
    # =========================================================================

    @staticmethod
    def _partition(
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        """将工具调用按并发安全性分组为批次。"""
        if not spec.concurrent_tools:
            return [[tc] for tc in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tc in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tc.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tc)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tc])
        if current:
            batches.append(current)
        return batches


# =============================================================================
# 文件编辑上下文（内部辅助类）
# =============================================================================

class _FileEditContext:
    """封装文件编辑追踪器和进度回调，消除重复的 if-not-None 判断。"""

    def __init__(
        self,
        trackers: list[StreamingFileEditTracker],
        progress_callback: Any,
    ) -> None:
        self._trackers = trackers
        self._progress = progress_callback

    async def emit_start(self, params: Any) -> None:
        await invoke_file_edit_progress(
            self._progress,
            [
                build_file_edit_start_event(
                    tracker,
                    params if isinstance(params, dict) else None,
                )
                for tracker in self._trackers
            ],
        )

    async def emit_end(self, params: Any) -> None:
        await invoke_file_edit_progress(
            self._progress,
            [
                build_file_edit_end_event(
                    tracker,
                    params if isinstance(params, dict) else None,
                )
                for tracker in self._trackers
            ],
        )

    async def emit_error(self, error: str) -> None:
        await invoke_file_edit_progress(
            self._progress,
            [build_file_edit_error_event(tracker, error) for tracker in self._trackers],
        )
