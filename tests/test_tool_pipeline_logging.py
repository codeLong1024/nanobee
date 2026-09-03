"""ToolPipeline [TOOL] 日志行 ctx 关联键测试（P2-2）。

覆盖场景：
1. [TOOL] 请求 / 结果 日志行携带 [ctx=<context_id>] 关联键
2. context_id 为空时日志行不携带关联键（不污染日志）
3. _log_ctx 辅助方法的返回值语义

关联键与审计插件 JSONL 记录的 user_id 同源（kernel 以 context_id 作为
用户上下文键），保证 debug.log 与审计 JSONL 两条日志流可互查。
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.agent.tool_pipeline import ToolPipeline
from nanobee.agent.specs import AgentRunSpec
from nanobee.providers.base import ToolCallRequest
from nanobee.utils.logger import logger


class _FakeTools:
    """最小工具注册表替身：execute 返回固定文本。"""

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        return f"result of {name}"


def _spec(context_id: str | None) -> AgentRunSpec:
    """构建最小可执行的 AgentRunSpec。"""
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=_FakeTools(),  # type: ignore[arg-type]
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=2000,
        context_id=context_id,
    )


def _tool_call() -> ToolCallRequest:
    return ToolCallRequest(id="call_1", name="read_file", arguments={"path": "a.txt"})


@pytest.fixture
def captured_logs():
    """捕获 loguru INFO 级别日志，返回 (消息列表, 清理函数)。"""
    messages: list[str] = []
    handler_id = logger.add(messages.append, level="INFO")
    yield messages
    logger.remove(handler_id)


class TestLogCtxHelper:
    """_log_ctx 关联键构建语义。"""

    def test_returns_ctx_prefix_with_context_id(self):
        assert ToolPipeline._log_ctx(_spec("yangbhi")) == "[ctx=yangbhi]"

    def test_returns_empty_without_context_id(self):
        assert ToolPipeline._log_ctx(_spec(None)) == ""


class TestToolLogCorrelationKey:
    """[TOOL] 日志行携带 ctx 关联键。"""

    @pytest.mark.asyncio
    async def test_request_and_result_logs_carry_ctx_key(self, captured_logs):
        """请求与结果日志行均包含 [ctx=<context_id>]。"""
        messages = captured_logs
        pipeline = ToolPipeline()

        result, _event, error = await pipeline.execute_one(
            _spec("yangbhi"), _tool_call(), {}, {},
        )

        assert error is None
        assert result == "result of read_file"
        joined = "\n".join(messages)
        assert "[TOOL][ctx=yangbhi] 请求: read_file" in joined
        assert "[TOOL][ctx=yangbhi] 结果: read_file" in joined

    @pytest.mark.asyncio
    async def test_no_ctx_key_without_context_id(self, captured_logs):
        """context_id 为空时日志行不含 [ctx= 片段。"""
        messages = captured_logs
        pipeline = ToolPipeline()

        await pipeline.execute_one(_spec(None), _tool_call(), {}, {})

        joined = "\n".join(messages)
        assert "[ctx=" not in joined
        # 原始日志结构不受影响
        assert "[TOOL] 请求: read_file" in joined
        assert "[TOOL] 结果: read_file" in joined

    @pytest.mark.asyncio
    async def test_truncated_result_still_carries_ctx_key(self, captured_logs):
        """结果超长被截断时关联键仍存在于日志行首。"""
        messages = captured_logs

        class _LongResultTools(_FakeTools):
            async def execute(self, name: str, params: dict[str, Any]) -> str:
                return "x" * 3000

        spec = _spec("yangbhi")
        spec.tools = _LongResultTools()  # type: ignore[assignment]
        pipeline = ToolPipeline()

        await pipeline.execute_one(spec, _tool_call(), {}, {})

        joined = "\n".join(messages)
        assert "[TOOL][ctx=yangbhi] 结果: read_file = " in joined
        assert "...(truncated)" in joined
