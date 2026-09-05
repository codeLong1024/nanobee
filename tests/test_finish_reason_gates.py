"""PR-A finish_reason 语义收紧测试。

验证 8 格场景矩阵中 PR-A 覆盖的行为：
  - error × 空/非空 → error 通道（final=None, error 非空）
  - blocked(content_filter/refusal) × 空/非空 → error 通道
  - normal 空 × N 次 → 带 tools 重试（不允许 tools=None 逼答）
  - truncated(length) × 空 → 诚实报 "empty final response"，不走 tools=None 逼答
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.agent.hook import AgentHook
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.providers.base import LLMResponse, map_finish_reason


def _build_spec(hook: AgentHook | None = None) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=ToolRegistry(),
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=65536,
        hook=hook,
        error_message="Sorry, I encountered an error calling the AI model.",
    )


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


class _CaptureHook(AgentHook):
    """Capture calls to check behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.iterations = 0


# ── 格 1/2: error × 空/非空 → error 通道 ──────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "some error text"])
async def test_error_cell_goes_to_error_channel(content):
    """error finish_reason 无论正文空/非空 → final=None, error 非空。"""
    provider = _StaticProvider(LLMResponse(
        content=content,
        finish_reason="error",
        error_kind="timeout",
    ))
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content is None
    assert result.error is not None


# ── 格 5/6: content_filter × 空/非空 → blocked 并入 error 通道 ────────

@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "filtered text"])
async def test_content_filter_goes_to_error_channel(content):
    """content_filter 归入 blocked → error 通道，final_content=None。"""
    provider = _StaticProvider(LLMResponse(
        content=content,
        finish_reason="content_filter",
    ))
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content is None
    assert result.error is not None


# ── 格 7/8: refusal × 空/非空 → blocked 并入 error 通道 ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "I refuse to do that"])
async def test_refusal_goes_to_error_channel(content):
    """refusal 归入 blocked → error 通道，final_content=None。"""
    provider = _StaticProvider(LLMResponse(
        content=content,
        finish_reason="refusal",
    ))
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content is None
    assert result.error is not None


# ── normal 空轮：应带 tools 重试（废除 tools=None 逼答）───────────────

@pytest.mark.asyncio
async def test_normal_empty_retry_keeps_tools():
    """normal(stop) 空响应 → 空重试和 finalization 必须携带 tools kwarg（不允许 tools=None 逼答）。"""
    # Sequence: 2 blanks (empty_content_retries reaches MAX=2) then finalization answer
    blank = LLMResponse(content=None, finish_reason="stop")
    answer = LLMResponse(content="final answer", finish_reason="stop")
    provider = _SequenceProvider(blank, blank, answer)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content == "final answer"
    assert result.error is None

    # All calls (including the finalization retry) must carry tools kwarg.
    # The 3rd call is the finalization retry triggered on iteration 1.
    assert len(provider.calls) >= 3
    finalization_call = provider.calls[2]
    assert "tools" in finalization_call
    assert finalization_call["tools"] is not None, "finalization retry must carry tools, not None"


# ── normal 空轮耗尽后应诚实报错而非编造 ──────────────────────────────

@pytest.mark.asyncio
async def test_normal_empty_exhausted_reports_empty_final():
    """normal 空响应永远无法恢复 → 诚实报 empty final response。"""
    blank = LLMResponse(content=None, finish_reason="stop")
    provider = _StaticProvider(blank)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content is None
    assert result.error == "empty final response"


# ── truncated(length) × 空 → 诚实报错，不走空重试 ────────────────────

@pytest.mark.asyncio
async def test_truncated_empty_goes_to_empty_final():
    """length 空正文：不进入空重试（不走 tools=None 逼答），直接报 empty final response。"""
    blank_length = LLMResponse(content=None, finish_reason="length")
    provider = _StaticProvider(blank_length)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    assert result.final_content is None
    assert result.error == "empty final response"


# ── truncated(length) × 非空：走 length 恢复（PR-A 保留现路径）────────

@pytest.mark.asyncio
async def test_truncated_non_empty_length_recovery():
    """length 非空正文 → 走 length 恢复路径（PR-A 不动 length 分支）。"""
    # First response: truncated with partial text
    truncated = LLMResponse(content="partial text", finish_reason="length")
    # Second response: completion
    completed = LLMResponse(content="complete answer", finish_reason="stop")
    provider = _SequenceProvider(truncated, completed)
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(_build_spec())

    # The recovery path appends partial text and recovery prompt, continues loop
    # until the next response delivers the full answer.
    assert result.final_content == "complete answer"
    assert result.error is None


# ── _classify_finish 对 blocked 返回 True ─────────────────────────────

def test_classify_finish_maps_blocked_to_error():
    """_classify_finish: content_filter/refusal → True (blocked 并入 error)。"""
    classify = AgentRunner._classify_finish

    # error
    assert classify("error") is True
    # blocked
    assert classify("content_filter") is True
    assert classify("refusal") is True
    # truncated — PR-A 不视为 error（由 length 恢复路径单独处理）
    assert classify("length") is False
    assert classify("max_tokens") is False
    # normal
    assert classify("stop") is False
    assert classify("tool_calls") is False
    assert classify("function_call") is False
    # unknown → normal（不拒执行）
    assert classify("weird_gateway_reason") is False
    # None → normal
    assert classify(None) is False


# ── map_finish_reason 归一化单测 ──────────────────────────────────────

@pytest.mark.parametrize("finish_reason,expected", [
    ("stop", "normal"),
    ("tool_calls", "normal"),
    ("function_call", "normal"),
    ("end_turn", "normal"),
    ("stop_sequence", "normal"),
    ("tool_use", "normal"),
    ("length", "truncated"),
    ("max_tokens", "truncated"),
    ("content_filter", "blocked"),
    ("refusal", "blocked"),
    ("error", "error"),
    (None, "normal"),
])
def test_map_finish_reason_aliases(finish_reason, expected):
    assert map_finish_reason(finish_reason) == expected


def test_map_finish_reason_unknown_to_normal():
    """未知枚举 → normal（拒绝必须显式列举，不误杀网关自定义词表）。"""
    assert map_finish_reason("gateway_specific_reason") == "normal"
