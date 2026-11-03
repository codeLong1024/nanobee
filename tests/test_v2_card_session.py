"""v2 结构化进度卡交付实现测试（CardHandle 所有权 + 推送泵 + 协议契约）。

覆盖场景：
1. open：成功返回携带独立会话的 handle；创建失败返回 None
2. 所有权：同 chat 的两个 handle 各自独立——交错事件不串卡、终态互不影响
3. 正文：按段累积；推送经泵节流（提交不等待网络）
4. 进度：start/end 更新步骤行状态（与正文共用同一泵）
5. 失败契约：fail 把错误文案写入正文并终态化（与 legacy 语义一致）
6. 协议形状：is_streaming / uses_emoji 等成员齐备
7. 推送泵：合并最新快照、终态 drain 冲刷、close 停止后台任务
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest

from nanobee.builtin.channel_dingtalk import v2_card_session as v2_module
from nanobee.builtin.channel_dingtalk.card_pipeline import CardHandle
from nanobee.builtin.channel_dingtalk.v2_card_renderer import V2CardDraft
from nanobee.builtin.channel_dingtalk.v2_card_session import V2CardPipeline


class _FakeV2Client:
    """替身客户端：记录 create/push 调用，可注入失败。"""

    instances: ClassVar[list[_FakeV2Client]] = []

    def __init__(
        self, card_client: Any, robot_code: str, target: dict[str, str], **kwargs: Any,
    ) -> None:
        self.robot_code = robot_code
        self.target = target
        self.kwargs = kwargs
        self.pushed: list[Any] = []
        self.create_error: Exception | None = None
        self.push_error: Exception | None = None
        self.created = False
        _FakeV2Client.instances.append(self)

    async def create(self, initial_param_map: dict[str, str]) -> str:
        if self.create_error:
            raise self.create_error
        self.created = True
        return f"card_{len(_FakeV2Client.instances)}"

    async def push(self, frame: Any) -> bool:
        if self.push_error:
            raise self.push_error
        self.pushed.append(frame)
        return True


@pytest.fixture
def fake_client_cls(monkeypatch: pytest.MonkeyPatch) -> type[_FakeV2Client]:
    """把管线内部的客户端类替换为替身，并清理实例记录。"""
    _FakeV2Client.instances = []
    monkeypatch.setattr(v2_module, "DingTalkV2CardClient", _FakeV2Client)
    return _FakeV2Client


def _pipeline(**kwargs: Any) -> V2CardPipeline:
    """构造管线（默认不节流，便于断言推送）。"""
    return V2CardPipeline(None, push_interval=kwargs.pop("push_interval", 0.0), **kwargs)


async def _open(pipeline: V2CardPipeline, chat_id: str = "chat-1") -> CardHandle:
    """开卡并断言返回了 handle。"""
    handle = await pipeline.open(
        chat_id, msg_id=f"msg-{chat_id}",
        robot_code="r1", target={"receiverUserId": "u1"},
    )
    assert handle is not None
    return handle


async def _settle() -> None:
    """让推送泵的后台任务完成一轮。"""
    await asyncio.sleep(0.01)


def _blocks(client: _FakeV2Client) -> list[dict]:
    """取替身最近一次 push 的 blockList。"""
    return json.loads(client.pushed[-1].card_param_map["blockList"])


@pytest.mark.asyncio
async def test_open_returns_handle_with_own_session(fake_client_cls: type) -> None:
    """open 成功：返回携带独立会话的 handle，且协议成员齐备。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)

    assert handle.card_id == "card_1"
    assert handle.chat_id == "chat-1"
    assert handle.msg_id == "msg-chat-1"
    assert handle.state is not None
    for method in (
        "open", "stream_delta", "progress", "stream_end",
        "finish", "fail", "is_streaming",
    ):
        assert callable(getattr(handle.pipeline, method))
    assert pipeline.is_streaming(handle) is True
    assert pipeline.uses_emoji is False
    await handle.state.pump.close()


@pytest.mark.asyncio
async def test_open_failure_returns_none(fake_client_cls: type) -> None:
    """创建失败：返回 None，不产生任何会话。"""
    pipeline = _pipeline()
    original_init = _FakeV2Client.__init__

    def failing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.create_error = RuntimeError("boom")

    _FakeV2Client.__init__ = failing_init  # type: ignore[method-assign]
    try:
        handle = await pipeline.open(
            "chat-1", msg_id="m", robot_code="r1", target={"receiverUserId": "u1"},
        )
    finally:
        _FakeV2Client.__init__ = original_init  # type: ignore[method-assign]
    assert handle is None


@pytest.mark.asyncio
async def test_fallback_metadata_has_no_legacy_card_id(fake_client_cls: type) -> None:
    """防复发（评审 #1）：v2 兜底 metadata 不得携带 legacy 卡片协议键 ``_card_id``。

    旧实现由通道层无条件把 v2 outTrackId 塞进 ``_card_id``，markdown 兜底时
    sender 会拿它执行 legacy 卡片终态原语（finalize_card/finish_streaming），
    对 v2 卡做不受控的跨协议写入。
    """
    pipeline = _pipeline()
    handle = await _open(pipeline)
    meta = pipeline.fallback_metadata(handle)
    assert meta == {"msg_id": "msg-chat-1"}
    assert "_card_id" not in meta
    await handle.state.pump.close()


@pytest.mark.asyncio
async def test_ownership_isolated_between_turns(fake_client_cls: type) -> None:
    """防复发（#1）：同 chat 的两个 handle 各自独立。

    交错事件不串卡、A 的终态不影响 B 的会话——所有权随 turn 流动，
    不存在任何按 chat_id 的共享查找表。
    """
    pipeline = _pipeline()
    handle_a = await _open(pipeline, "chat-1")
    handle_b = await _open(pipeline, "chat-1")
    assert handle_a.state is not handle_b.state

    await pipeline.stream_delta(handle_a, "A 的正文")
    await _settle()
    client_a = _FakeV2Client.instances[0]
    client_b = _FakeV2Client.instances[1]
    assert client_a.pushed[-1].card_param_map["content"] == "A 的正文"
    assert client_b.pushed == []  # B 的卡片未被 A 的事件污染

    assert await pipeline.finish(handle_a, content="A 完成", mode="complete") is True
    assert client_a.pushed[-1].finished is True
    assert pipeline.is_streaming(handle_b) is True  # A 终态不波及 B

    await pipeline.stream_delta(handle_b, "B 的正文")
    await _settle()
    assert client_b.pushed[-1].card_param_map["content"] == "B 的正文"
    await handle_b.state.pump.close()


@pytest.mark.asyncio
async def test_stream_delta_accumulates_and_throttles(fake_client_cls: type) -> None:
    """正文：节流窗口内只推一帧（合并最新快照），终态冲刷携带完整正文。"""
    pipeline = _pipeline(push_interval=3600.0)
    handle = await _open(pipeline)
    client = _FakeV2Client.instances[-1]

    assert await pipeline.stream_delta(handle, "第一段") is True
    assert await pipeline.stream_delta(handle, "第二段") is True
    await _settle()
    assert len(client.pushed) == 1  # 窗口内合并为一帧
    assert client.pushed[0].streaming_content == "第一段第二段"

    assert await pipeline.finish(handle, content="", mode="append") is True
    assert client.pushed[-1].finished is True
    assert client.pushed[-1].card_param_map["content"] == "第一段第二段"


@pytest.mark.asyncio
async def test_stream_delta_never_blocks_caller(fake_client_cls: type) -> None:
    """防复发（#3）：提交不等待网络——push 尚未返回时调用方即可继续。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    client = _FakeV2Client.instances[-1]
    release = asyncio.Event()
    entered = asyncio.Event()

    async def blocked_push(frame: Any) -> bool:
        entered.set()
        await release.wait()
        client.pushed.append(frame)
        return True

    client.push = blocked_push  # type: ignore[method-assign]
    assert await pipeline.stream_delta(handle, "delta") is True  # 未被网络阻塞
    await entered.wait()
    release.set()
    await _settle()
    assert len(client.pushed) == 1
    await handle.state.pump.close()


@pytest.mark.asyncio
async def test_progress_updates_steps(fake_client_cls: type) -> None:
    """工具事件：start 追加执行中步骤，end 原地更新为已完成。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    client = _FakeV2Client.instances[-1]

    assert await pipeline.progress(
        handle, [{"phase": "start", "call_id": "c1", "name": "read_file"}],
    ) is True
    await _settle()
    assert _blocks(client)[0]["markdown"] == "read_file · 执行中"

    assert await pipeline.progress(
        handle, [{"phase": "end", "call_id": "c1", "name": "read_file"}],
    ) is True
    await _settle()
    assert _blocks(client)[0]["markdown"] == "read_file · 已完成"
    await handle.state.pump.close()


@pytest.mark.asyncio
async def test_fail_puts_error_text_on_card(fake_client_cls: type) -> None:
    """防复发（#2）：失败终态必须让错误文案出现在卡片正文里。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    await pipeline.stream_delta(handle, "半截进度")
    await _settle()

    assert await pipeline.fail(handle, "LLM 调用失败：上游超时") is True
    frame = _FakeV2Client.instances[-1].pushed[-1]
    assert frame.finished is True
    # 半截进度保留 + 错误原因可见（原实现只把 error 用作状态行标签）
    assert "半截进度" in frame.card_param_map["content"]
    assert "LLM 调用失败：上游超时" in frame.card_param_map["content"]
    assert pipeline.is_streaming(handle) is False


@pytest.mark.asyncio
async def test_fail_without_partial_only_error_text(fake_client_cls: type) -> None:
    """无半截进度的纯失败：正文即错误文案。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    assert await pipeline.fail(handle, "内部错误") is True
    frame = _FakeV2Client.instances[-1].pushed[-1]
    assert frame.card_param_map["content"].endswith("内部错误")


@pytest.mark.asyncio
async def test_terminal_rejects_late_events(fake_client_cls: type) -> None:
    """终态后：迟到事件与重复终态全部静默拒绝，不再推送。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    assert await pipeline.finish(handle, content="done", mode="complete") is True
    client = _FakeV2Client.instances[-1]
    pushed_after_finish = len(client.pushed)

    assert await pipeline.stream_delta(handle, "迟到") is False
    assert await pipeline.progress(
        handle, [{"phase": "start", "call_id": "c", "name": "n"}],
    ) is False
    assert await pipeline.finish(handle, content="再来", mode="complete") is False
    assert await pipeline.fail(handle, "又失败") is False
    await _settle()
    assert len(client.pushed) == pushed_after_finish


@pytest.mark.asyncio
async def test_stream_end_is_noop_for_v2(fake_client_cls: type) -> None:
    """流暂停/结束：v2 卡片常显正文，无需保活，直接返回 False。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    assert await pipeline.stream_end(handle, resuming=True) is False
    assert await pipeline.stream_end(handle, resuming=False) is False
    await handle.state.pump.close()


@pytest.mark.asyncio
async def test_pump_merges_pending_snapshots(fake_client_cls: type) -> None:
    """推送泵：节流窗口内的多帧合并为最新快照，不放大 API 调用。"""
    pipeline = _pipeline(push_interval=3600.0)
    handle = await _open(pipeline)
    for text in ("一", "二", "三"):
        await pipeline.stream_delta(handle, text)
        await asyncio.sleep(0)
    await _settle()
    client = _FakeV2Client.instances[-1]
    assert len(client.pushed) == 1
    assert client.pushed[0].streaming_content == "一"

    # 待发快照已合并为最新累积值：终态冲刷时体现完整正文
    assert await pipeline.finish(handle, content="", mode="append") is True
    assert client.pushed[-1].card_param_map["content"] == "一二三"


@pytest.mark.asyncio
async def test_pump_close_stops_task_and_rejects_submit(fake_client_cls: type) -> None:
    """close：取消后台任务、停止接受提交。"""
    pipeline = _pipeline(push_interval=3600.0)
    handle = await _open(pipeline)
    await pipeline.stream_delta(handle, "x")
    await asyncio.sleep(0)

    pump = handle.state.pump
    await pump.close()
    assert pump._task is None

    pump.submit(handle.state.draft.frame())  # 已关闭：静默忽略
    await _settle()
    assert len(_FakeV2Client.instances[-1].pushed) <= 1


@pytest.mark.asyncio
async def test_pump_push_failure_is_swallowed(fake_client_cls: type) -> None:
    """推送失败：泵吞掉异常（不炸热路径），终态 drain 以 False 上报。"""
    pipeline = _pipeline()
    handle = await _open(pipeline)
    handle.state.pump._client.push_error = RuntimeError("api down")

    await pipeline.stream_delta(handle, "delta")
    await _settle()
    assert await pipeline.finish(handle, content="done", mode="complete") is False


class _LogRecorder:
    """替身日志器：计数 exception/warning 调用（验证日志去洪）。"""

    def __init__(self) -> None:
        self.exception_calls = 0
        self.warning_calls = 0

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self.exception_calls += 1

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls += 1

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_pump_failure_still_throttles(fake_client_cls: type) -> None:
    """防复发（评审 #3）：失败同样推进节流时钟，故障期不按 delta 速率放大请求。

    旧实现只在成功时更新 _last_push_at，卡片接口持续报错时每个 delta
    都立即重试（2 次 PUT + 完整 traceback），仅被 20 QPS 令牌桶钳制。
    """
    pipeline = _pipeline(push_interval=3600.0)
    handle = await _open(pipeline)
    client = _FakeV2Client.instances[-1]
    client.push_error = RuntimeError("api down")

    attempts = 0
    original_push = client.push

    async def counting_push(frame: Any) -> bool:
        nonlocal attempts
        attempts += 1
        return await original_push(frame)

    client.push = counting_push  # type: ignore[method-assign]

    await pipeline.stream_delta(handle, "第一段")
    await _settle()
    await pipeline.stream_delta(handle, "第二段")
    await _settle()
    assert attempts == 1  # 窗口内失败帧同样被节流合并

    # 终态冲刷不受节流约束：仍会尝试推送（并如实上报 False）
    assert await pipeline.finish(handle, content="done", mode="complete") is False
    assert attempts == 2


@pytest.mark.asyncio
async def test_pump_consecutive_failure_log_dedup(fake_client_cls: type) -> None:
    """防复发（评审 #3）：连续失败仅首帧打完整堆栈，后续降级为 warning 计数。"""
    client = _FakeV2Client(None, "r1", {})
    recorder = _LogRecorder()
    pump = v2_module.CardFramePump(client, interval=0.0, log=recorder)
    frame = V2CardDraft(show_metadata=False).frame()

    client.push_error = RuntimeError("api down")
    for _ in range(3):
        pump.submit(frame)
        await _settle()
    assert recorder.exception_calls == 1
    assert recorder.warning_calls == 2

    # 成功后计数归零：再次失败重新记首帧完整堆栈
    client.push_error = None
    pump.submit(frame)
    await _settle()
    client.push_error = RuntimeError("api down again")
    pump.submit(frame)
    await _settle()
    assert recorder.exception_calls == 2
    await pump.close()
