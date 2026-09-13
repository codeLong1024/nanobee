"""Agent 执行相关的数据类、类型定义与工具函数。

将 AgentRunSpec、AgentRunResult、PluginHooks 等共享类型从 runner.py 提取到此模块，
避免 tool_pipeline.py → runner.py → tool_pipeline.py 的循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypedDict

from nanobee.agent.hook import AgentHook
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger

# 纯技术诊断串（不带致歉前缀）：用户可见的致歉文案由 turn_internal_error 通知模板统一包装，
# 避免模板文案 + 此兜底文案叠加造成"双重道歉"。
_DEFAULT_ERROR_MESSAGE = "LLM 调用失败：模型返回错误且未提供诊断内容。"


class ExitReason(str, Enum):
    """Agent 迭代循环的退出原因（纯控制流，与成功/失败正交）。

    Attributes:
        COMPLETED: 循环自然走完（成功或失败都算 completed，失败语义由 error 承载）。
        MAX_ITERATIONS: 触达迭代上限。
        CANCELLED: 被外部取消（/stop、/new 等）。
        ABANDONED: turn 未产出 runner 结果（状态机异常/取消/关停排空），
            由 loop 在 ``_process_message`` 的 finally 兜底盖章——保证
            「每 turn 恰好一份终态 report」的框架不变量。
    """

    COMPLETED = "completed"
    MAX_ITERATIONS = "max_iterations"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class PluginHooks(TypedDict, total=False):
    """插件 Hook 回调字典。

    Attributes:
        pre_invoke: 工具执行前拦截钩子，签名 (call_id: str, tool_name: str, args: dict) → args
        post_invoke: 工具执行后拦截钩子，签名 (call_id: str, tool_name: str, result: Any) → result
    """

    pre_invoke: list[Callable[[str, str, dict[str, Any]], dict[str, Any]]]
    post_invoke: list[Callable[[str, str, Any], Any]]


@dataclass(slots=True)
class AgentRunSpec:
    """Agent 单次执行的配置。"""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    context_id: str | None = None
    trace_id: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    filtered_tool_names: list[str] | None = None
    plugin_hooks: PluginHooks | None = None
    # 通道上下文（用于工具插件的 set_context 调用）
    channel: str = ""
    chat_id: str = ""
    sender_id: str = ""
    session_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    # 需要节流的外部查询工具名集合，从插件 metadata.throttle_group 构建
    throttled_tool_names: dict[str, str] = field(default_factory=dict)
    # 具有命令执行能力的工具名集合，从插件 metadata.exec_capable 构建
    exec_capable_tools: set[str] = field(default_factory=set)
    # 具有文件编辑能力的工具名集合，从插件 metadata.file_edit_capability 构建
    file_edit_tools: set[str] = field(default_factory=set)


@dataclass(slots=True)
class IterationFact:
    """单轮 LLM 迭代事实（runner 已算好的值，只搬运不重算）。

    账本纪律：只记索引/计数/耗时/ID，不拷贝消息内容。

    Attributes:
        no: 迭代序号（0 起）。
        llm_call_ms: 本轮 LLM 往返耗时（毫秒，perf_counter 口径）。
        finish_reason: 原始 finish_reason（is_error 分类前的原值）。
        usage: 本轮 raw_usage（``_usage_dict`` 输出的浅拷贝，防下游篡改）。
        tool_call_ids: 本轮工具调用 ID（关联插件侧 tool span）。
    """

    no: int
    llm_call_ms: float = 0.0
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tool_call_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InjectionFact:
    """一次排空注入事实。

    Attributes:
        count: 本次注入消息条数。
        phase: drain 触发阶段标识（如 ``"after tool execution"``）。
    """

    count: int
    phase: str = ""


@dataclass(slots=True)
class TurnLedger:
    """runner 侧 turn 记账本（append-only，无逻辑）。

    runner 在已计算事实的现成代码行旁 O(1) 追加，随 AgentRunResult 发布；
    loop 在 turn 边界盖章合成 :class:`TurnReport`。本类只承载事实，
    不做任何聚合或推断——聚合语义由消费者（如 audit 插件）自行映射。

    Attributes:
        turn_input_index: 本轮用户输入在 initial_messages 中的下标
            （``len(initial_messages) - 1``），消息窗口起点锚。这是显式契约：
            initial_messages 末元素必为本轮用户输入（由通道层过滤空消息保证，
            空输入不得进入 turn），且 runner 对消息列表只 append 不删改。
        iterations: 逐轮迭代事实。
        injections: 逐次排空注入事实。
        exit_reason: 退出原因字符串（exit_reason.value），唯一 return 处盖章。
        error: 失败诊断（None 表示成功）。
    """

    turn_input_index: int = 0
    iterations: list[IterationFact] = field(default_factory=list)
    injections: list[InjectionFact] = field(default_factory=list)
    exit_reason: str = ""
    error: str | None = None


@dataclass(slots=True)
class TurnReport:
    """loop 组装的 turn 结账单（completed hook payload）。

    loop 拥有 turn 生命周期所以盖章（turn_id / turn_started_at），
    runner 拥有核心窗口事实所以记账（ledger）。Phase 2 将扩展
    abandoned 终态语义，本类是盖章载体。

    Attributes:
        turn_id: turn 唯一标识（复用归一化后的 trace_id，不新增身份概念）。
        turn_started_at: ISO 墙钟（loop dispatch 时刻，覆盖队列等待+上下文构建）。
        turn_ended_at: ISO 墙钟（loop 结账时刻，与 turn_started_at 同源同钟）。
        ledger: runner 侧记账本（引用，非拷贝）。
        messages_window: 本轮消息窗口（``result.messages[turn_input_index:]``
            切片浅拷贝（新 list，元素 dict 与 result.messages 共享引用），
            消费者不得改写元素；result.messages 为最终态，turn 结束后
            runner 不再修改）。
    """

    turn_id: str
    turn_started_at: str
    ledger: TurnLedger
    turn_ended_at: str = ""
    messages_window: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class AgentRunResult:
    """Agent 单次执行的最终结果。

    Attributes:
        final_content: 只有"成功回复"，失败时为 None（失败语义由 error 承载）。
        messages: 真实对话 + 工具协议，不包含错误占位符。
        exit_reason: 循环退出原因（控制流，与成功/失败正交）。
        error: 唯一"失败"权威字段，为 None 表示成功。
        ledger: runner 侧 turn 记账本（增量字段，发布 runner 已算好的事实）。
    """

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    exit_reason: ExitReason = ExitReason.COMPLETED
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    ledger: TurnLedger = field(default_factory=TurnLedger)
