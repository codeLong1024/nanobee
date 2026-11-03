"""工具进度事件（start/end）通知测试。

覆盖场景：
1. 工具成功执行 → progress_callback 依次收到 phase=start 与 phase=end 事件，
   end 事件按 call_id 关联、detail 为结果摘要
2. 工具抛异常 → phase=error，error 字段携带错误详情
3. message 工具（LLM 输出投递载体）不产生 start/end 事件
4. progress_callback 为 None 时静默跳过，不影响执行
5. 守卫/预备阶段拦截（未真正执行）不产生 end 事件

背景：``_notify_tool_end`` 补齐了 ``build_tool_event_finish_payloads``
一直缺失的生产调用方，使钉钉 A2UI 进度卡能把步骤行从「执行中」
原地更新为「已完成/失败」。
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.agent.specs import SILENT_PROGRESS_TOOLS, AgentRunSpec
from nanobee.agent.tool_pipeline import ToolPipeline
from nanobee.providers.base import ToolCallRequest


class _FakeTools:
    """最小工具注册表替身：execute 返回固定文本或按名字抛异常。"""

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        if name == "boom_tool":
            raise RuntimeError("boom details")
        return f"result of {name}"


def _spec(progress_callback: Any = None) -> AgentRunSpec:
    """构建最小可执行的 AgentRunSpec。"""
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=_FakeTools(),  # type: ignore[arg-type]
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=2000,
        progress_callback=progress_callback,
        silent_progress_tools=SILENT_PROGRESS_TOOLS,
    )


def _tool_call(call_id: str = "call_1", name: str = "read_file") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments={"path": "a.txt"})


def _events_of(callback_log: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """从回调日志中摊平全部 tool_events。"""
    return [event for _, kwargs in callback_log for event in (kwargs.get("tool_events") or [])]


async def _capture_callback(
    log: list[tuple[str, dict[str, Any]]],
) -> Any:
    """构造记录 (content, kwargs) 的 progress_callback。"""

    async def on_progress(content: str, **kwargs: Any) -> None:
        log.append((content, kwargs))

    return on_progress


class TestNotifyToolEnd:
    """工具结束事件（phase=end/error）通知契约。"""

    @pytest.mark.asyncio
    async def test_success_emits_start_then_end(self) -> None:
        """成功路径：先 start 后 end，end 的 phase/call_id/name/detail 正确。"""
        log: list[tuple[str, dict[str, Any]]] = []
        pipeline = ToolPipeline()
        await pipeline.execute_one(_spec(await _capture_callback(log)), _tool_call(), {}, {})

        events = _events_of(log)
        assert [e["phase"] for e in events] == ["start", "end"]
        end = events[-1]
        assert end["call_id"] == "call_1"
        assert end["name"] == "read_file"
        assert "result of read_file" in end["detail"]
        # end 事件不携带完整结果对象，摘要走 detail
        assert end["result"] is None
        assert end["files"] == [] and end["embeds"] == []

    @pytest.mark.asyncio
    async def test_exception_emits_error_phase(self) -> None:
        """工具抛异常：end 变为 phase=error，error 字段携带详情。"""
        log: list[tuple[str, dict[str, Any]]] = []
        pipeline = ToolPipeline()
        await pipeline.execute_one(
            _spec(await _capture_callback(log)),
            _tool_call(name="boom_tool"),
            {}, {},
        )

        events = _events_of(log)
        assert [e["phase"] for e in events] == ["start", "error"]
        assert events[-1]["error"]
        assert "boom" in events[-1]["error"]

    @pytest.mark.asyncio
    async def test_message_tool_silent(self) -> None:
        """message 工具不产生任何进度事件。"""
        log: list[tuple[str, dict[str, Any]]] = []
        pipeline = ToolPipeline()
        await pipeline.execute_one(
            _spec(await _capture_callback(log)),
            _tool_call(name="message"),
            {}, {},
        )
        assert _events_of(log) == []

    @pytest.mark.asyncio
    async def test_no_progress_callback_still_executes(self) -> None:
        """progress_callback 为 None：静默跳过通知，执行结果不受影响。"""
        pipeline = ToolPipeline()
        result_str, event, fatal = await pipeline.execute_one(
            _spec(None), _tool_call(), {}, {},
        )
        assert "result of read_file" in result_str
        assert event["status"] == "ok"
        assert fatal is None

    @pytest.mark.asyncio
    async def test_guard_intercept_emits_no_end(self) -> None:
        """守卫拦截（未真正执行、未发 start）：不产生 end 事件。"""
        log: list[tuple[str, dict[str, Any]]] = []

        async def on_progress(content: str, **kwargs: Any) -> None:
            log.append((content, kwargs))

        spec = AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=_FakeTools(),  # type: ignore[arg-type]
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=2000,
            progress_callback=on_progress,
            filtered_tool_names=frozenset({"read_file"}),
            silent_progress_tools=SILENT_PROGRESS_TOOLS,
        )
        pipeline = ToolPipeline()
        await pipeline.execute_one(spec, _tool_call(name="boom_tool"), {}, {})
        events = _events_of(log)
        assert events == []
