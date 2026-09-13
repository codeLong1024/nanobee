"""TurnLedger 记账本测试：验证 runner 在现成代码点发布已算好的 turn 事实。

核心不变量：
1. 多轮迭代 → 每轮一条 IterationFact（finish_reason 原值 / usage / tool_call_ids）；
2. 排空注入 → InjectionFact（count + phase），注入条数不再坍缩成 bool；
3. 所有退出路径在唯一 return 盖 exit_reason / error 章；
4. run() 异常折叠路径产生带 error 的空账本兜底；
5. loop → runner → 插件 全链路：TurnReport 被送达 on_message_completed。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from nanobee.agent.hook import AgentHook
from nanobee.agent.loop import AgentLoop, TurnContext, TurnState
from nanobee.agent.messages import InboundMessage
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.specs import TurnReport
from nanobee.agent.tools.base import Tool, tool_parameters
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.plugins.base import NanobeePlugin, PluginMetadata
from nanobee.providers.base import LLMResponse, ToolCallRequest


@tool_parameters({
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
class _EchoTool(Tool):
    """返回固定文本的测试工具。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo test tool"

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs.get("text", "echo")


class _ToolThenStopProvider:
    """第一轮返回工具调用，第二轮返回最终回复。"""

    supports_progress_deltas = False

    def __init__(self) -> None:
        self.calls = 0

    def _resp(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="call_1", name="echo", arguments={"text": "hi"}),
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 11, "completion_tokens": 3},
            )
        return LLMResponse(
            content="done",
            finish_reason="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5},
        )

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._resp(**kwargs)

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._resp(**kwargs)


class _ToolCallForeverProvider:
    """每次都返回工具调用的 provider（驱动循环触达 max_iterations 出口）。"""

    supports_progress_deltas = False

    @staticmethod
    def _resp() -> LLMResponse:
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="call_x", name="echo", arguments={"text": "hi"}),
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 5, "completion_tokens": 1},
        )

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._resp()

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return self._resp()


class _ErrorProvider:
    """返回 finish_reason="error" 的 provider。"""

    supports_progress_deltas = False

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="Error calling LLM: timed out",
            finish_reason="error",
            error_kind="timeout",
        )

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content="Error calling LLM: timed out",
            finish_reason="error",
            error_kind="timeout",
        )


class _BoomHook(AgentHook):
    """在迭代开始前抛异常，测试 run() 异常折叠路径的账本兜底。"""

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: Any) -> None:
        raise RuntimeError("boom in hook")


def _build_spec(
    hook: AgentHook,
    *,
    injection_callback: Any = None,
    tools: ToolRegistry | None = None,
) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools or ToolRegistry(),
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=65536,
        hook=hook,
        error_message="Sorry, I encountered an error calling the AI model.",
        injection_callback=injection_callback,
    )


@pytest.mark.asyncio
async def test_multi_iteration_records_iteration_facts():
    """多轮迭代 → 每轮一条 IterationFact，退出路径盖章。"""
    tools = ToolRegistry()
    tools.register(_EchoTool())
    runner = AgentRunner(_ToolThenStopProvider())

    result = await runner.run(_build_spec(AgentHook(), tools=tools))

    ledger = result.ledger
    assert ledger.exit_reason == "completed"
    assert ledger.error is None
    # 窗口起点锚：单条 user 输入 → 下标 0
    assert ledger.turn_input_index == 0
    # 两条迭代事实：工具调用轮 + 最终回复轮
    assert len(ledger.iterations) == 2
    first, second = ledger.iterations
    assert first.no == 0
    assert first.tool_call_ids == ["call_1"]
    assert first.finish_reason == "tool_calls"
    assert first.usage == {"prompt_tokens": 11, "completion_tokens": 3}
    assert first.llm_call_ms >= 0.0
    assert second.no == 1
    assert second.finish_reason == "stop"
    assert second.tool_call_ids == []
    assert second.usage == {"prompt_tokens": 20, "completion_tokens": 5}


@pytest.mark.asyncio
async def test_injection_recorded_with_count_and_phase():
    """排空注入 → InjectionFact(count, phase)，注入行为可数。"""

    drain_rounds = {"n": 0}

    async def injection_callback(limit: int = 3) -> list[dict[str, Any]]:
        drain_rounds["n"] += 1
        if drain_rounds["n"] == 1:
            return [{"role": "user", "content": "follow-up"}]
        return []

    provider = _ToolThenStopProvider()
    # 第一轮带工具调用：工具执行后（"after tool execution"）排空到注入 → 继续；
    # 第二轮返回最终回复，排空为空 → 正常结束。
    runner = AgentRunner(provider)
    result = await runner.run(
        _build_spec(AgentHook(), injection_callback=injection_callback),
    )

    assert result.had_injections is True
    assert len(result.ledger.injections) == 1
    fact = result.ledger.injections[0]
    assert fact.count == 1
    assert fact.phase == "after tool execution"
    assert result.ledger.exit_reason == "completed"


@pytest.mark.asyncio
async def test_llm_error_path_stamps_ledger():
    """LLM 错误路径：迭代事实记录原值 finish_reason，出口盖章 error。"""
    runner = AgentRunner(_ErrorProvider())
    result = await runner.run(_build_spec(AgentHook()))

    assert result.error is not None
    ledger = result.ledger
    assert ledger.exit_reason == "completed"
    assert ledger.error is not None
    assert len(ledger.iterations) == 1
    assert ledger.iterations[0].finish_reason == "error"


@pytest.mark.asyncio
async def test_exception_fold_produces_error_ledger():
    """run() 异常折叠路径：空迭代事实 + error 兜底账本。"""
    runner = AgentRunner(_ToolThenStopProvider())
    result = await runner.run(_build_spec(_BoomHook()))

    assert result.final_content is None
    assert result.error is not None
    assert "boom in hook" in result.error
    assert result.ledger.iterations == []
    assert result.ledger.injections == []
    assert result.ledger.error is not None
    assert "boom in hook" in result.ledger.error
    assert result.ledger.exit_reason == "completed"


@pytest.mark.asyncio
async def test_exception_fold_anchors_window_to_turn_input():
    """异常路径窗口锚点：turn_input_index 锚定本轮输入而非归零（评审 F2 回归）。

    历史污染回归防线：异常折叠复用 run() 入口创建的同一账本，
    turn_input_index 保持 len(initial_messages)-1，loop 侧切片
    ``messages[turn_input_index:]`` 不会把 system/历史记成本轮窗口。
    """
    runner = AgentRunner(_ToolThenStopProvider())
    spec = AgentRunSpec(
        initial_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        tools=ToolRegistry(),
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=65536,
        hook=_BoomHook(),
    )
    result = await runner.run(spec)

    assert result.error is not None
    assert result.ledger.turn_input_index == 1


@pytest.mark.asyncio
async def test_max_iterations_drain_recorded():
    """触达迭代上限后的排空也必须记账（评审 F4 回归：第 7 处排空点）。

    注入消息写入了 messages 且 had_injections 置 True，
    InjectionFact 必须同步产生，账本与 had_injections 不得自相矛盾。
    """
    drain_calls = {"n": 0}

    async def injection_callback(limit: int = 3) -> list[dict[str, Any]]:
        drain_calls["n"] += 1
        # 迭代期间排空为空（不触发续跑），只在触达上限后的收尾排空给出注入
        if drain_calls["n"] >= 3:
            return [{"role": "user", "content": "late follow-up"}]
        return []

    provider = _ToolCallForeverProvider()
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=ToolRegistry(),
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=65536,
        hook=AgentHook(),
        injection_callback=injection_callback,
    )
    result = await AgentRunner(provider).run(spec)

    assert result.exit_reason.value == "max_iterations"
    assert result.had_injections is True
    assert len(result.ledger.injections) == 1
    fact = result.ledger.injections[0]
    assert fact.count == 1
    assert fact.phase == "after max_iterations"


class _ReportCapturePlugin(NanobeePlugin):
    """捕获 on_message_completed 收到的 TurnReport。"""

    async def on_message_completed(self, context: Any, report: TurnReport) -> None:
        self.reports.append(report)

    def __init__(self, metadata: Any = None) -> None:
        if metadata is None:
            metadata = PluginMetadata(name="report_capture", plugin_type="audit")
        super().__init__(metadata)
        self.reports: list[TurnReport] = []


class _FakePluginManager:
    """模拟 PluginManager，手动注入插件列表。"""

    def __init__(self) -> None:
        self._plugins: list[NanobeePlugin] = []

    def get_enabled_plugins(self) -> list[NanobeePlugin]:
        return self._plugins

    def add(self, plugin: NanobeePlugin) -> None:
        self._plugins.append(plugin)


class _FakeContextManager:
    """模拟 ContextManager，返回带 user_id 的轻量上下文。"""

    async def get_or_create(self, context_id: str) -> Any:
        ctx = type("_Ctx", (), {})()
        ctx.context_id = context_id
        ctx.user_id = context_id
        return ctx


def _make_loop_skeleton(provider: Any, plugins: list[NanobeePlugin]) -> AgentLoop:
    """构造最小可用的 AgentLoop（绕过 __init__ 的重依赖装配）。"""
    pm = _FakePluginManager()
    for p in plugins:
        pm.add(p)
    loop = object.__new__(AgentLoop)
    loop.runner = AgentRunner(provider)
    loop.tools = ToolRegistry()
    loop.model = "test-model"
    loop.max_iterations = 3
    loop.max_tool_result_chars = 65536
    loop.workspace = Path("/tmp")
    loop.context_window_tokens = 200_000
    loop.context_block_limit = 0
    loop.provider_retry_mode = "simple"
    loop._extra_hooks = []
    loop._pending_blockers = {}
    loop._hook_tasks = set()
    loop._throttled_tool_groups = {}
    loop._exec_capable_tools = set()
    loop._file_edit_tools = set()
    loop.plugin_manager = pm
    loop.context_manager = _FakeContextManager()
    return loop


@pytest.mark.asyncio
async def test_loop_delivers_turn_report_to_plugin():
    """全链路：loop 盖章（turn_id/started_at）+ runner 账本 + 本轮消息窗口 → 插件。

    这是「把手里的真值发布出去」的端到端断言：插件收到的 report 里
    迭代事实、窗口切片、turn 身份全部来自框架，而非插件反推。
    """
    plugin = _ReportCapturePlugin()
    loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
    trace_id = "b" * 32
    started_at = time.time()

    initial_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    result = await loop._run_agent_loop(
        initial_messages,
        context_id="ctx-1",
        trace_id=trace_id,
        turn_started_at=started_at,
    )
    # 等 fire-and-forget 的 non-blocking Hook 消费完
    await asyncio.sleep(0.05)

    assert len(plugin.reports) == 1
    report = plugin.reports[0]
    # 身份与起点：loop 盖章
    assert report.turn_id == trace_id
    assert datetime.fromisoformat(report.turn_started_at) is not None
    # 账本：runner 事实直通（迭代 + usage + 退出原因）
    assert [f.no for f in report.ledger.iterations] == [0, 1]
    assert report.ledger.iterations[0].usage["prompt_tokens"] == 11
    assert report.ledger.exit_reason == "completed"
    assert report.ledger.error is None
    # 消息窗口：从本轮 user 输入切片（不含 system/历史）
    assert report.messages_window == result[2][1:]
    assert report.messages_window[0] == {"role": "user", "content": "hi"}


class TestTurnTerminalGuarantee:
    """Phase 2 终态保证：每 turn 恰好一份终态 report（含取消/异常兜底）。"""

    def _make_turn(self, trace_id: str) -> TurnContext:
        msg = InboundMessage(
            channel="test", sender_id="u1", chat_id="c1", content="hi",
        )
        return TurnContext(
            msg=msg,
            context_id="ctx-1",
            session_id="default",
            state=TurnState.RESTORE,
            turn_id=f"ctx-1:{time.time_ns()}",
            trace_id=trace_id,
        )

    @pytest.mark.asyncio
    async def test_emit_turn_report_is_idempotent(self):
        """_emit_turn_report 幂等：同一 turn 第二次调用不双发。"""
        plugin = _ReportCapturePlugin()
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
        turn = self._make_turn("c" * 32)

        for reason in ("first", "second"):
            await loop._emit_turn_report(
                turn=turn, context_id="ctx-1", trace_id=turn.trace_id,
                turn_started_at=time.time(),
                result=None, abandon_error=reason,
            )
        await asyncio.sleep(0.05)

        assert turn.turn_report_emitted is True
        assert len(plugin.reports) == 1

    @pytest.mark.asyncio
    async def test_abandoned_report_synthesized_from_turn_input(self):
        """兜底终态：ABANDONED 账本 + 输入侧窗口 + error 恒非 None。"""
        plugin = _ReportCapturePlugin()
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
        turn = self._make_turn("d" * 32)
        turn.initial_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]

        await loop._emit_turn_report(
            turn=turn, context_id="ctx-1", trace_id=turn.trace_id,
            turn_started_at=turn.turn_wall_started_at,
            result=None, abandon_error="turn cancelled before completion",
        )
        await asyncio.sleep(0.05)

        report = plugin.reports[0]
        assert report.turn_id == turn.trace_id
        assert report.ledger.exit_reason == "abandoned"
        assert report.ledger.error is not None
        assert "cancelled" in report.ledger.error
        # 窗口仅含输入侧：锚定本轮 user 输入（不含 system/历史）
        assert report.messages_window == [{"role": "user", "content": "hi"}]
        assert datetime.fromisoformat(report.turn_ended_at) is not None

    @pytest.mark.asyncio
    async def test_cancelled_turn_emits_abandoned_report(self):
        """取消 E2E：状态机内 CancelledError → finally 兜底落 ABANDONED。"""
        plugin = _ReportCapturePlugin()
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
        loop._refresh_provider_snapshot = lambda: None

        async def _cancel_in_restore(ctx):
            raise asyncio.CancelledError()

        # 实例属性遮蔽方法：RESTORE 状态即被取消（模拟 /stop）
        loop._state_restore = _cancel_in_restore

        msg = InboundMessage(
            channel="test", sender_id="u1", chat_id="c1", content="hi",
        )
        with pytest.raises(asyncio.CancelledError):
            await loop._process_message(msg, context_id="ctx-1")

        # 兜底任务是 create_task 登记的 fire-and-forget，经 drain 落地
        assert await loop.drain_hook_tasks(timeout_s=2.0) == 0
        assert len(plugin.reports) == 1
        report = plugin.reports[0]
        assert report.ledger.exit_reason == "abandoned"
        assert "cancelled" in (report.ledger.error or "")
        assert len(report.turn_id) == 32

    @pytest.mark.asyncio
    async def test_abandoned_report_uses_outer_context_root(self, tmp_path, monkeypatch):
        """取消/异常路径的兜底结账拿到外层 context_root（评审 #2 回归）。

        context_root 只在 _state_run 内绑定，兜底任务创建时内层绑定已复位；
        _process_message 外层补绑后，ABANDONED 审计必须读到用户目录而非
        /tmp 进程级回退目录。
        """
        from nanobee.kernel.context_manager import ContextManager
        from nanobee.kernel.context_sandbox_var import current_context_root

        plugin = _ReportCapturePlugin()
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
        loop._refresh_provider_snapshot = lambda: None

        class _KernelStub:
            """ContextManager 所需的最小 kernel 桩。"""

            def __init__(self, data_dir: Path) -> None:
                self.config = {"data_dir": str(data_dir)}
                self.data_dir = data_dir
                self.event_bus = None

        cm = ContextManager(_KernelStub(tmp_path))
        loop.context_manager = cm
        expected_root = (await cm.get_or_create("ctx-1")).context_root

        # 捕获兜底任务执行时的 context_root（create_task 复制创建时刻上下文）
        captured: dict[str, Any] = {}
        original_emit = loop._emit_turn_report

        async def _spy_emit(**kwargs: Any):
            captured["root"] = current_context_root()
            return await original_emit(**kwargs)

        monkeypatch.setattr(loop, "_emit_turn_report", _spy_emit)

        async def _boom_restore(ctx):
            raise ValueError("restore boom")

        async def _boom_respond(ctx):
            raise ValueError("respond boom")

        # RESTORE 抛错 → 状态机恢复跳 RESPOND → RESPOND 再抛错 → 不可恢复，
        # 异常穿透触发 finally 兜底 ABANDONED。
        loop._state_restore = _boom_restore
        loop._state_respond = _boom_respond

        msg = InboundMessage(
            channel="test", sender_id="u1", chat_id="c1", content="hi",
        )
        with pytest.raises(ValueError):
            await loop._process_message(msg, context_id="ctx-1")

        await asyncio.sleep(0.05)
        assert captured["root"] == expected_root

    @pytest.mark.asyncio
    async def test_normal_run_with_turn_emits_single_report(self):
        """正常路径经 turn 结账一次，finally 兜底不再双发。"""
        plugin = _ReportCapturePlugin()
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [plugin])
        turn = self._make_turn("e" * 32)

        await loop._run_agent_loop(
            [{"role": "user", "content": "hi"}],
            context_id="ctx-1",
            trace_id=turn.trace_id,
            turn_started_at=time.time(),
            turn=turn,
        )
        await asyncio.sleep(0.05)
        assert len(plugin.reports) == 1
        assert turn.turn_report_emitted is True
        assert plugin.reports[0].ledger.exit_reason == "completed"

        # 幂等：兜底路径再触发也不会双发
        await loop._emit_turn_report(
            turn=turn, context_id="ctx-1", trace_id=turn.trace_id,
            turn_started_at=time.time(),
            result=None, abandon_error="should not emit",
        )
        await asyncio.sleep(0.05)
        assert len(plugin.reports) == 1

    @pytest.mark.asyncio
    async def test_drain_hook_tasks_reports_pending_count(self):
        """drain 有界等待：超时后返回未完成任务数。"""
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [])
        fut = asyncio.get_running_loop().create_future()

        async def _hang():
            await fut

        loop._track_hook_task(asyncio.create_task(_hang()))
        try:
            assert await loop.drain_hook_tasks(timeout_s=0.05) == 1
        finally:
            fut.cancel()
            await asyncio.sleep(0.01)
        # 任务收口后登记集合自清
        assert loop._hook_tasks == set()

    @pytest.mark.asyncio
    async def test_drain_hook_tasks_drains_child_spawned_during_wait(self):
        """静默排空（评审 #3 回归）：等待期内派生的子任务也必须被等到。

        模拟结账任务在运行中 create_task 派生落盘子任务：单次快照等待
        会在父任务完成即返回（此时子任务仍在落盘），静默循环必须等到
        子任务完成才返回 0。
        """
        loop = _make_loop_skeleton(_ToolThenStopProvider(), [])
        child_done = asyncio.Event()

        async def _parent():
            async def _child():
                await asyncio.sleep(0.05)
                child_done.set()

            loop._track_hook_task(asyncio.create_task(_child()))

        loop._track_hook_task(asyncio.create_task(_parent()))

        assert await loop.drain_hook_tasks(timeout_s=2.0) == 0
        assert child_done.is_set()
        assert loop._hook_tasks == set()
