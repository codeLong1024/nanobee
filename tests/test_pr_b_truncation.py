"""PR-B 调用级截断与处方恢复测试。

覆盖评审文档 PR-B 计划：
- 格 4（length × 非空 + 完整工具 A + 截断工具 B）：A 执行、B 收到 is_error result、
  恢复提示携带截断工具名
- provider 层在 json_repair 之前保留 arguments_raw
- 长度恢复次数上限改为 2
- 恢复耗尽后输出框架消息（不走模型）
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.agent.hook import AgentHook
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.tools.base import Tool
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.providers.base import LLMResponse, ToolCallRequest, classify_finish_reason
from nanobee.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    TRUNCATED_ARGS_ERROR_MESSAGE,
    TRUNCATED_TOOL_ADVICE,
    build_length_recovery_message,
)


# ── 测试工具 ─────────────────────────────────────────────────────────────

class _QueryTool(Tool):
    """测试用查询工具，返回固定文本。"""

    @property
    def name(self) -> str:
        return "query_sql"

    @property
    def description(self) -> str:
        return "查询 SQL 数据库"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> Any:
        return f"SQL result for: {kwargs.get('query', '')}"


class _EchoTool(Tool):
    """测试用简单工具。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回声工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> Any:
        return f"echo: {kwargs.get('text', '')}"


def _registry_with_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_QueryTool())
    reg.register(_EchoTool())
    return reg


def _build_spec(hook: AgentHook | None = None) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=_registry_with_tools(),
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=65536,
        hook=hook,
        error_message="Sorry, I encountered an error calling the AI model.",
    )


# ── Mock providers ────────────────────────────────────────────────────────

class _SequenceProvider:
    """Returns responses in sequence; last response repeats."""

    supports_progress_deltas = False

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self._responses[-1]

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        return await self.chat_with_retry(**kwargs)


class _StaticProvider:
    """Returns a single fixed LLMResponse for every call."""

    supports_progress_deltas = False

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        return self._response

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        return self._response


# ── Provider parse helper ────────────────────────────────────────────────

def _tool_call(
    name: str,
    args: dict[str, Any],
    raw: str | None = None,
    call_id: str | None = None,
) -> ToolCallRequest:
    """构造一个 ToolCallRequest，可选带 arguments_raw。"""
    return ToolCallRequest(
        id=call_id or f"call_{name}",
        name=name,
        arguments=args,
        arguments_raw=raw,
    )


# ── 格 4：length × 非空 + 完整工具 A + 截断工具 B ─────────────────────

@pytest.mark.asyncio
async def test_length_valid_tool_executes_truncated_tool_gets_error():
    """完整工具 A 执行，截断工具 B 收到 error tool result。

    构造 length + 完整工具 A(query_sql) + 截断工具 B(query_sql)。
    场景：模型生成工具调用时被输出长度截断，A 参数完整、B 参数被截断。
    """
    # 完整工具 A: args 严格 json 可解析
    tool_a = _tool_call(
        "query_sql",
        {"query": "SELECT 1"},
        raw='{"query": "SELECT 1"}',
        call_id="call_a",
    )
    # 截断工具 B: raw args 不完整 JSON（{"query": "SELECT ... AND date >
    tool_b = _tool_call(
        "query_sql",
        # json_repair 可能会把截断修复成合法 dict
        {"query": "SELECT * FROM t WHERE date >"},
        raw='{"query": "SELECT * FROM t WHERE date >',  # 截断 - 缺少闭合 }
        call_id="call_b",
    )

    # First response: truncated with tools and partial content
    truncated = LLMResponse(
        content="Let me query the data...",
        tool_calls=[tool_a, tool_b],
        finish_reason="length",
    )
    # Second response: model completes with final answer after recovery
    completed = LLMResponse(
        content="The query result is complete.",
        finish_reason="stop",
    )
    provider = _SequenceProvider(truncated, completed)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    # Valid tool A should have been executed (its result appears in messages)
    # Truncated tool B should NOT execute but get an error result
    assert result.final_content == "The query result is complete."
    assert result.error is None

    # Check that the messages contain a tool result for the valid tool A
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) >= 2  # one for A execution, one for B error

    # A's result should be present (actual execution result)
    a_results = [m for m in tool_messages if m.get("tool_call_id") == tool_a.id]
    assert a_results, "Valid tool A should have a tool result"
    assert "SQL result for" in str(a_results[0].get("content", ""))

    # B's result should be the truncation error
    b_results = [m for m in tool_messages if m.get("tool_call_id") == tool_b.id]
    assert b_results, "Truncated tool B should have an error tool result"
    assert TRUNCATED_ARGS_ERROR_MESSAGE in str(b_results[0].get("content", ""))


@pytest.mark.asyncio
async def test_truncated_round_tools_used_tracked():
    """truncated 轮的工具调用被正确跟踪到 tools_used。"""
    tool_a = _tool_call(
        "query_sql",
        {"query": "SELECT 1"},
        raw='{"query": "SELECT 1"}',
        call_id="call_a",
    )
    tool_b = _tool_call(
        "query_sql",
        {"query": "incomplete"},
        raw='{"query": "incomplete',  # 截断
        call_id="call_b",
    )

    truncated = LLMResponse(
        content="Let me query...",
        tool_calls=[tool_a, tool_b],
        finish_reason="length",
    )
    completed = LLMResponse(content="Done.", finish_reason="stop")
    provider = _SequenceProvider(truncated, completed)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    # Both tools should be tracked
    assert "query_sql" in result.tools_used


@pytest.mark.asyncio
async def test_truncated_dispatch_emits_tools_completed_checkpoint():
    """PR-B 截断分发在 awaiting_tools 之后应补发 tools_completed checkpoint。"""
    tool_a = _tool_call(
        "query_sql",
        {"query": "SELECT 1"},
        raw='{"query": "SELECT 1"}',
        call_id="call_a",
    )
    tool_b = _tool_call(
        "query_sql",
        {"query": "incomplete"},
        raw='{"query": "incomplete',  # 截断
        call_id="call_b",
    )

    truncated = LLMResponse(
        content="Let me query...",
        tool_calls=[tool_a, tool_b],
        finish_reason="length",
    )
    completed = LLMResponse(content="Done.", finish_reason="stop")

    phases: list[str] = []

    async def _on_checkpoint(payload: dict) -> None:
        phases.append(payload.get("phase", ""))

    provider = _SequenceProvider(truncated, completed)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=_registry_with_tools(),
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=65536,
        checkpoint_callback=_on_checkpoint,
    )
    await runner.run(spec)

    # 截断分发路径应先 awaiting_tools，再 tools_completed。
    assert "awaiting_tools" in phases
    assert "tools_completed" in phases
    assert phases.index("tools_completed") > phases.index("awaiting_tools")


# ── 参数化长度恢复消息 ──────────────────────────────────────────────────

def test_build_length_recovery_message_with_tool_names():
    """build_length_recovery_message 带工具名时生成处方化文案。"""
    msg = build_length_recovery_message(tool_names=["query_sql"])
    assert msg["role"] == "user"
    assert "query_sql" in msg["content"]
    assert "truncated" in msg["content"] or "截断" in msg["content"]


def test_build_length_recovery_message_without_tool_names():
    """无工具名时保持原有通用文案。"""
    msg = build_length_recovery_message()
    assert msg["role"] == "user"
    assert "Output limit reached" in msg["content"]


def test_build_length_recovery_message_empty_tool_names():
    """空列表传 None → 通用文案。"""
    msg = build_length_recovery_message(tool_names=[])
    assert "Output limit reached" in msg["content"]


def test_build_length_recovery_message_applies_shrink_advice():
    """带截断工具名时命中 TRUNCATED_TOOL_ADVICE 收缩建议，不再引导原样重发。"""
    msg = build_length_recovery_message(tool_names=["write_file"])
    content = msg["content"]
    # 处方是"缩小操作"而非"完整重发"，命中映射工具给出具体收缩建议。
    assert TRUNCATED_TOOL_ADVICE["write_file"].split("——")[0] in content
    assert "narrow" in content or "缩小" in content


def test_build_length_recovery_message_dedups_tool_names():
    """同名多次截断只给出一次建议，且每条建议均被保留。"""
    msg = build_length_recovery_message(tool_names=["edit_file", "edit_file", "write_file"])
    content = msg["content"]
    for tool in ("edit_file", "write_file"):
        assert f"- {tool}:" in content


def test_build_length_recovery_message_falls_back_to_generic_shrink():
    """未命中映射的工具名回落到通用收缩处方，仍含工具名与收缩语义。"""
    msg = build_length_recovery_message(tool_names=["query_sql"])
    content = msg["content"]
    assert "query_sql" in content
    # 通用回落同样引导收窄操作，而非重发完整参数。
    assert "narrow" in content or "缩小" in content


def test_empty_final_response_message_is_honest():
    """兜底文案不得编造"工具步骤已完成"，应如实说明本轮未产出最终答案。"""
    assert "couldn't produce a final answer" in EMPTY_FINAL_RESPONSE_MESSAGE
    assert "completed the tool steps" not in EMPTY_FINAL_RESPONSE_MESSAGE


# ── 截断参数判定 ────────────────────────────────────────────────────────

def test_has_truncated_arguments_detects_bad_json():
    """arguments_raw 无法被严格 json.loads → 截断。"""
    tc = _tool_call("query_sql", {}, raw='{"query": "SELECT ...')
    assert AgentRunner._has_truncated_arguments(tc) is True


def test_has_truncated_arguments_accepts_valid_json():
    """arguments_raw 可被严格 json.loads → 非截断。"""
    tc = _tool_call("query_sql", {"query": "SELECT 1"}, raw='{"query": "SELECT 1"}')
    assert AgentRunner._has_truncated_arguments(tc) is False


def test_has_truncated_arguments_none_raw_is_not_truncated():
    """arguments_raw 为 None（provider 传入 dict）→ 非截断。"""
    tc = _tool_call("query_sql", {"query": "SELECT 1"})
    assert AgentRunner._has_truncated_arguments(tc) is False


def test_has_truncated_arguments_empty_dict_is_ok():
    """空 dict 的 arguments_raw → json.loads('{}') 成功 → 非截断。"""
    tc = _tool_call("echo", {}, raw="{}")
    assert AgentRunner._has_truncated_arguments(tc) is False


# ── 长度恢复次数上限 2 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_length_recovery_exhausted_emits_framework_message():
    """连续 3 次截断 → 第 1/2 次走恢复，第 3 次超过上限输出框架消息。

    关键断言：不会把第 3 次的部分内容当最终答复交付，而是输出框架消息。
    """
    # All 3 responses are truncated with partial content, no tools
    truncated_1 = LLMResponse(content="partial response 1", finish_reason="length")
    truncated_2 = LLMResponse(content="partial response 2", finish_reason="length")
    truncated_3 = LLMResponse(content="partial response 3", finish_reason="length")

    # Use a sequence provider so the runner sees the same truncated response repeatedly
    provider_seq = _SequenceProvider(truncated_1, truncated_2, truncated_3)
    runner_seq = AgentRunner(provider_seq)  # type: ignore[arg-type]
    result = await runner_seq.run(_build_spec())

    # Recovery budget is 2, so on the 3rd truncated response we hit exhaustion
    # Should produce a framework message, not the model's partial content.
    # ctx.error 携带单一文案源（turn_truncated 中文目录文案），经 loop 透传为用户可见 detail。
    assert result.error is not None
    assert "截断" in result.error

    # Framework honest message is the final content (not model output)
    assert "截断" in result.final_content

    # The 3rd partial model content should NOT appear as a standalone assistant message
    content_3 = "partial response 3"
    msg_contents = [str(m.get("content", "")) for m in result.messages if m.get("role") == "assistant"]
    assert content_3 not in msg_contents, (
        "Third truncated response should not be delivered as final content"
    )


# ── Provider arguments_raw 保存 ────────────────────────────────────────

def test_tool_call_arguments_raw_default_is_none():
    """ToolCallRequest.arguments_raw 默认值为 None。"""
    tc = ToolCallRequest(
        id="call_1",
        name="query_sql",
        arguments={"query": "SELECT 1"},
    )
    assert tc.arguments_raw is None


def test_tool_call_arguments_raw_round_trip():
    """ToolCallRequest.to_openai_tool_call 不含 arguments_raw。"""
    tc = _tool_call(
        "query_sql",
        {"query": "SELECT 1"},
        raw='{"query": "SELECT 1"}',
    )
    serialized = tc.to_openai_tool_call()
    # arguments_raw should not leak into the serialized tool call
    assert "arguments_raw" not in str(serialized)


# ── 未知 finish_reason 保持 normal ─────────────────────────────────────

def test_max_tokens_maps_to_truncated():
    """max_tokens → truncated（与 length 同档）。"""
    assert classify_finish_reason("max_tokens") == "truncated"


# ── 流式 path: SSE chunks 截断 ────────────────────────────────────────────

def test_parse_chunks_sets_arguments_raw_for_streaming():
    """_parse_chunks 在流式聚合后保存 arguments_raw。"""
    from nanobee.providers.openai_compat_provider import OpenAICompatProvider

    # Simulate SSE chunks for a tool call with complete arguments
    class _FakeChunk:
        def __init__(self, choices=None):
            self.choices = choices or []

    class _FakeChoice:
        def __init__(self, delta, finish_reason=None):
            self.delta = delta
            self.finish_reason = finish_reason

    class _FakeDelta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class _FakeToolCall:
        def __init__(self, index, id_, fn_name, fn_args):
            self.index = index
            self.id = id_
            self.function = _FakeFunction(fn_name, fn_args)

    class _FakeFunction:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    # Two chunks: first with name, second with arguments
    tc1 = _FakeToolCall(0, "call_1", "query_sql", '')
    delta1 = _FakeDelta(content=None, tool_calls=[tc1])
    chunk1 = _FakeChunk([_FakeChoice(delta1)])

    tc2 = _FakeToolCall(0, None, None, '{"query": "SELECT 1"}')
    delta2 = _FakeDelta(content=None, tool_calls=[tc2])
    chunk2 = _FakeChunk([_FakeChoice(delta2)])

    finish_chunk = _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="stop")])

    response = OpenAICompatProvider._parse_chunks([chunk1, chunk2, finish_chunk])

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.arguments_raw == '{"query": "SELECT 1"}'
    assert tc.arguments == {"query": "SELECT 1"}


def test_parse_chunks_sets_arguments_raw_for_truncated_stream():
    """_parse_chunks 在截断的流式 chunk 后 arguments_raw 保留不完整 JSON。"""
    from nanobee.providers.openai_compat_provider import OpenAICompatProvider

    class _FakeChunk:
        def __init__(self, choices=None):
            self.choices = choices or []

    class _FakeChoice:
        def __init__(self, delta, finish_reason=None):
            self.delta = delta
            self.finish_reason = finish_reason

    class _FakeDelta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class _FakeToolCall:
        def __init__(self, index, id_, fn_name, fn_args):
            self.index = index
            self.id = id_
            self.function = _FakeFunction(fn_name, fn_args)

    class _FakeFunction:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    # Tool call name arrives, then args arrive partially and get cut off
    tc1 = _FakeToolCall(0, "call_1", "query_sql", '')
    chunk1 = _FakeChunk([_FakeChoice(_FakeDelta(tool_calls=[tc1]))])

    tc2 = _FakeToolCall(0, None, None, '{"query": "SELECT * FROM')
    chunk2 = _FakeChunk([_FakeChoice(_FakeDelta(tool_calls=[tc2]))])

    # Stream ends with finish_reason=length (truncated)
    finish_chunk = _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason="length")])

    response = OpenAICompatProvider._parse_chunks([chunk1, chunk2, finish_chunk])

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.arguments_raw == '{"query": "SELECT * FROM'
    assert response.finish_reason == "length"


def test_parse_non_streaming_sets_arguments_raw():
    """_parse 非流式解析保存 arguments_raw。"""
    from nanobee.providers.openai_compat_provider import OpenAICompatProvider

    # Build a mock response object with tool_calls that have string arguments
    class _FakeFunction:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _FakeToolCall:
        def __init__(self, function):
            self.function = function

    class _FakeMsg:
        def __init__(self):
            self.content = "test"
            self.tool_calls = [
                _FakeToolCall(_FakeFunction("query_sql", '{"query": "SELECT 1"}'))
            ]
            self.reasoning_content = None
            self.reasoning = None

    class _FakeChoice:
        def __init__(self):
            self.message = _FakeMsg()
            self.finish_reason = "tool_calls"

    class _FakeResponse:
        def __init__(self):
            self.choices = [_FakeChoice()]
            self.usage = None

    provider = OpenAICompatProvider(api_key="test")
    response = provider._parse(_FakeResponse())

    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.arguments_raw == '{"query": "SELECT 1"}'
    assert tc.arguments == {"query": "SELECT 1"}
    assert response.finish_reason == "tool_calls"
