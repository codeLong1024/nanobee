"""Runner 错误流结束语义测试。

核心行为：当 LLM 返回 finish_reason="error"（超时等）时，
错误语义唯一由 ``AgentRunResult.error`` 承载；runner 的 ``on_stream_end``
只表达"流结束"传输信号（resuming），不夹带 error。错误时跳过
on_stream_end，卡片停在 INPUTING，由 loop 的 fail_card 唯一完结。
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.agent.hook import AgentHook
from nanobee.agent.runner import AgentRunner, AgentRunSpec
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.providers.base import LLMResponse


class _CaptureHook(AgentHook):
    """记录 on_stream_end 调用，模拟钉钉流式通道。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def wants_streaming(self) -> bool:
        return True

    async def on_stream_end(self, context: Any, *, resuming: bool) -> None:
        self.calls.append({"resuming": resuming})


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


class _BoomProvider:
    """正常返回，但 hook 抛异常时测试 run() 的异常折叠。"""

    supports_progress_deltas = False

    async def chat_stream_with_retry(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="hello", finish_reason="stop")

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="hello", finish_reason="stop")


class _BoomHook(AgentHook):
    """在迭代开始前抛异常，模拟 _run_core 内部意外异常。"""

    def wants_streaming(self) -> bool:
        return False

    async def before_iteration(self, context: Any) -> None:
        raise RuntimeError("boom in hook")


def _build_spec(hook: AgentHook) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=ToolRegistry(),
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=65536,
        hook=hook,
        error_message="Sorry, I encountered an error calling the AI model.",
    )


@pytest.mark.asyncio
async def test_llm_error_does_not_trigger_on_stream_end():
    """LLM finish_reason="error" 时，on_stream_end 不应被触发。

    错误是"流未开始就中止"，不是"流结束"。runner 跳过 on_stream_end，
    卡片停在 INPUTING，由 loop 的 fail_card 唯一完结（避免空 FINISHED 残留
    与双路径二次终态化）。错误语义只由 result.error 承载。
    """
    hook = _CaptureHook()
    runner = AgentRunner(_ErrorProvider())

    result = await runner.run(_build_spec(hook))

    # 失败语义由 error 承载，final_content 为空
    assert result.final_content is None
    assert result.error is not None
    assert "Error calling LLM" in result.error
    # 错误分支不触发 on_stream_end（流并未真正结束）
    assert hook.calls == [], "错误分支不得触发 on_stream_end（卡片由 fail_card 完结）"


@pytest.mark.asyncio
async def test_run_folds_internal_exception_into_error_not_raise():
    """run() 内部意外异常统一折叠进 result.error，不再 re-raise。

    这是"统一归集程序异常"的契约：run() 要么正常返回（含 error 字段），
    要么 CancelledError，绝不把内部异常 re-raise 给调用方叠加两套错误处理。
    """
    runner = AgentRunner(_BoomProvider())
    hook = _BoomHook()

    result = await runner.run(_build_spec(hook))

    # 未 re-raise，正常返回；error 承载异常诊断
    assert result is not None
    assert result.final_content is None
    assert result.error is not None
    assert "boom in hook" in result.error
