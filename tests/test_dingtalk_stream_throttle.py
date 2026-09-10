"""Tests for DingTalk card streaming throttle (stream_push_min_interval).

覆盖场景：
- 间隔内增量仅累积不推送（节流生效）
- 首帧始终立即推送
- 间隔流逝后恢复推送
- _stream_end 终态全量推送兜底（节流期间未推内容不丢失）
- resuming=True 重置节流状态（下个流式段首帧立即推）
- stream_push_min_interval=0 退化为逐 delta 推送（旧行为回归保护）
- per-card 隔离
- 错误路径 take_stream_buffer 同步清理节流时间戳
"""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobee.builtin.channel_dingtalk.card_manager import CardManager
from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
from nanobee.builtin.channel_dingtalk.sender import DingTalkSender


# ============================================================
# Fixtures
# ============================================================


class _FakeClock:
    """可控制的 monotonic 时钟，避免真实 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def fake_clock() -> _FakeClock:
    clock = _FakeClock()
    with patch("nanobee.builtin.channel_dingtalk.sender.time", clock):
        yield clock


def make_sender(min_interval: float) -> DingTalkSender:
    """构造一个 mock 化的 DingTalkSender（不触网）。"""
    s = DingTalkSender.__new__(DingTalkSender)
    s.config = DingTalkConfig(
        client_id="test-id",
        client_secret="test-secret",
        stream_push_min_interval=min_interval,
    )
    s.logger = MagicMock()
    s._http = MagicMock()
    s._card_manager = MagicMock()
    s._card_manager.stream_content = AsyncMock()
    s._card_manager.finish_streaming = AsyncMock()
    s._card_manager.finish_card_status = AsyncMock()
    s._streaming_buffers = {}
    s._last_push_ts = {}
    s._overflow_cards = set()
    s._streamed_cards: OrderedDict[str, bool] = OrderedDict()
    s._card_has_streamed = set()
    s._emotion_contexts = {}
    s._token_manager = MagicMock()
    s._token_manager.get_access_token = AsyncMock(return_value="fake-token")
    s._send_batch_message = AsyncMock(return_value=True)
    return s


def make_delta_msg(content: str, card_id: str) -> SimpleNamespace:
    """构造 channel.py _on_stream 形态的流式增量消息。"""
    return SimpleNamespace(
        channel="channel_dingtalk",
        chat_id="conv-test",
        content=content,
        metadata={"_stream_delta": True, "_card_id": card_id, "msg_id": "msg-001"},
        media=[],
    )


def make_stream_end_msg(card_id: str, *, resuming: bool) -> SimpleNamespace:
    return SimpleNamespace(
        channel="channel_dingtalk",
        chat_id="conv-test",
        content="",
        metadata={"_stream_end": True, "_card_id": card_id, "_resuming": resuming,
                  "msg_id": "msg-001"},
        media=[],
    )


async def send_deltas(sender: DingTalkSender, card_id: str, *chunks: str) -> None:
    for chunk in chunks:
        await sender.send(make_delta_msg(chunk, card_id))


# ============================================================
# Throttle behavior
# ============================================================


class TestStreamPushThrottle:
    @pytest.mark.asyncio
    async def test_throttle_suppresses_rapid_deltas(self, fake_clock):
        """间隔内连续增量只推首帧，buffer 仍累积全量。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "你好", "，世界", "！")

        assert sender._card_manager.stream_content.await_count == 1
        assert sender._streaming_buffers["card-1"] == "你好，世界！"
        assert "card-1" in sender._card_has_streamed

    @pytest.mark.asyncio
    async def test_first_delta_pushes_immediately(self, fake_clock):
        """首帧（buffer 为空）无条件立即推送。"""
        sender = make_sender(min_interval=60.0)
        await send_deltas(sender, "card-1", "首帧")

        sender._card_manager.stream_content.assert_awaited_once_with("card-1", "首帧")

    @pytest.mark.asyncio
    async def test_push_after_interval_elapsed(self, fake_clock):
        """间隔流逝后，下一增量恢复推送，且推送全量累积内容。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "第一帧")
        assert sender._card_manager.stream_content.await_count == 1

        fake_clock.now += 1.5  # 越过间隔
        await send_deltas(sender, "card-1", "第二帧")

        assert sender._card_manager.stream_content.await_count == 2
        sender._card_manager.stream_content.assert_awaited_with("card-1", "第一帧第二帧")

    @pytest.mark.asyncio
    async def test_stream_end_pushes_full_buffer(self, fake_clock):
        """节流期间未推送的内容由 _stream_end 终态全量推送兜底。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "A", "B", "C")
        assert sender._card_manager.stream_content.await_count == 1  # 仅首帧

        await sender.send(make_stream_end_msg("card-1", resuming=False))

        # 首帧 1 次 + 终态全量 1 次 = 2 次，终态内容为完整拼接
        assert sender._card_manager.stream_content.await_count == 2
        sender._card_manager.stream_content.assert_awaited_with("card-1", "ABC")
        sender._card_manager.finish_streaming.assert_awaited_once_with("card-1", "ABC")
        # 终态后 buffer 与时间戳均清理
        assert "card-1" not in sender._streaming_buffers
        assert "card-1" not in sender._last_push_ts

    @pytest.mark.asyncio
    async def test_resuming_resets_throttle(self, fake_clock):
        """工具调用暂停（resuming=True）后，新流式段首帧立即推送。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "第一轮")
        await sender.send(make_stream_end_msg("card-1", resuming=True))

        assert sender._card_manager.stream_content.await_count == 1  # 第一轮首帧
        assert "card-1" not in sender._streaming_buffers
        assert "card-1" not in sender._last_push_ts

        await send_deltas(sender, "card-1", "第二轮")
        # 新段首帧：不间隔等待，立即推送（共 2 次）
        assert sender._card_manager.stream_content.await_count == 2
        sender._card_manager.stream_content.assert_awaited_with("card-1", "第二轮")

    @pytest.mark.asyncio
    async def test_zero_interval_keeps_legacy_behavior(self, fake_clock):
        """stream_push_min_interval=0 退化为逐 delta 推送（旧行为回归保护）。"""
        sender = make_sender(min_interval=0.0)
        await send_deltas(sender, "card-1", "A", "B", "C")

        assert sender._card_manager.stream_content.await_count == 3
        sender._card_manager.stream_content.assert_awaited_with("card-1", "ABC")

    @pytest.mark.asyncio
    async def test_per_card_isolation(self, fake_clock):
        """节流窗口按 card_id 隔离，互不影响。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-a", "A1")
        await send_deltas(sender, "card-b", "B1")  # 不同卡片首帧，立即推
        await send_deltas(sender, "card-a", "A2")  # 同卡片间隔内，抑制

        assert sender._card_manager.stream_content.await_count == 2
        assert sender._streaming_buffers["card-a"] == "A1A2"
        assert sender._streaming_buffers["card-b"] == "B1"

    @pytest.mark.asyncio
    async def test_take_stream_buffer_cleans_timestamp(self, fake_clock):
        """错误路径 take_stream_buffer 同步清理节流时间戳，无状态泄漏。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "半截")
        assert "card-1" in sender._last_push_ts

        content = sender.take_stream_buffer("card-1")

        assert content == "半截"
        assert "card-1" not in sender._streaming_buffers
        assert "card-1" not in sender._last_push_ts

    @pytest.mark.asyncio
    async def test_finalize_card_with_notification_cleans_timestamp(self, fake_clock):
        """max_iterations 兜底路径同步清理节流时间戳。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "碎片")
        assert "card-1" in sender._last_push_ts

        await sender.finalize_card_with_notification("card-1", "msg-001", "已达轮次上限")

        assert "card-1" not in sender._streaming_buffers
        assert "card-1" not in sender._last_push_ts

    @pytest.mark.asyncio
    async def test_overflow_branch_not_throttled(self, fake_clock):
        """buffer 溢出分支立即推全量，不受节流抑制。"""
        sender = make_sender(min_interval=60.0)
        sender.config.stream_buffer_max_chars = 5
        await send_deltas(sender, "card-1", "AAAAAA")  # 首帧即溢出

        assert sender._card_manager.stream_content.await_count == 1
        assert "card-1" in sender._overflow_cards


# ============================================================
# Stream end failure semantics — 终态置位失败不得重复投递
# ============================================================


class TestStreamEndFailureSemantics:
    """终态置位失败 ≠ 内容未送达，markdown 兜底仅限内容渲染步失败。"""

    @pytest.mark.asyncio
    async def test_finish_failure_no_markdown_duplicate(self, fake_clock):
        """复现实测事故：stream_content 200 + finish_streaming 500 system.busy
        → 降级 finish_card_status，绝不发 markdown。"""
        sender = make_sender(min_interval=1.0)
        sender._card_manager.finish_streaming = AsyncMock(
            side_effect=Exception("500 system.busy"),
        )
        await send_deltas(sender, "card-1", "A", "B", "C")
        await sender.send(make_stream_end_msg("card-1", resuming=False))

        sender._send_batch_message.assert_not_awaited()  # 用户不收第二份
        sender._card_manager.finish_card_status.assert_awaited_once_with("card-1")
        assert "card-1" in sender._streamed_cards  # 标记已流式处理，通道不再兜底

    @pytest.mark.asyncio
    async def test_finish_and_status_both_fail_no_markdown(self, fake_clock):
        """终态与状态降级均失败：内容已可见，仍不重复投递。"""
        sender = make_sender(min_interval=1.0)
        sender._card_manager.finish_streaming = AsyncMock(
            side_effect=Exception("500 system.busy"),
        )
        sender._card_manager.finish_card_status = AsyncMock(
            side_effect=Exception("500 system.busy"),
        )
        await send_deltas(sender, "card-1", "内容")
        await sender.send(make_stream_end_msg("card-1", resuming=False))

        sender._send_batch_message.assert_not_awaited()
        assert "card-1" in sender._streamed_cards

    @pytest.mark.asyncio
    async def test_stream_content_failure_falls_back_to_markdown(self, fake_clock):
        """内容渲染步失败 = 卡片无内容，markdown 兜底是唯一正确动作。"""
        sender = make_sender(min_interval=1.0)
        await send_deltas(sender, "card-1", "A")
        sender._card_manager.stream_content = AsyncMock(
            side_effect=Exception("network error"),
        )
        await sender.send(make_stream_end_msg("card-1", resuming=False))

        sender._send_batch_message.assert_awaited_once()  # markdown 兜底恰一次
        args = sender._send_batch_message.await_args
        assert args[0][2] == "sampleMarkdown"
        assert args[0][3]["text"] == "A"


# ============================================================
# CardManager 失败语义 — 渲染失败才返回 False
# ============================================================


def _make_card_manager(put_side_effects: list) -> CardManager:
    """构造 mock 化 CardManager，put 按 side_effect 序列响应。"""
    inner_client = MagicMock()
    inner_client.put = AsyncMock(side_effect=put_side_effects)
    client = MagicMock()
    client.ensure_async_client = AsyncMock(return_value=inner_client)
    client.get_headers_async = AsyncMock(return_value={})
    client.put = inner_client.put  # 断言计数与 ensure_async_client 返回的同一实例
    client.check_response = AsyncMock()
    return CardManager(client)


class TestCardManagerFailureSemantics:
    def _ok_resp(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"success": true}'
        return resp

    @pytest.mark.asyncio
    async def test_fail_card_finish_failure_returns_true(self):
        """错误文案上屏成功 + finish 500 → 返回 True（文案已可见，
        调用方不得 markdown 重复投递），内部降级重试状态置位。"""
        mgr = _make_card_manager([
            self._ok_resp(),               # stream_content 成功
            Exception("500 system.busy"),  # finish_streaming 失败
            self._ok_resp(),               # finish_card_status 降级成功
        ])
        assert await mgr.fail_card("card-1", "出错了") is True
        assert mgr.client.put.await_count == 3

    @pytest.mark.asyncio
    async def test_fail_card_stream_failure_returns_false(self):
        """渲染步失败 = 文案未上屏，返回 False 让调用方 markdown 兜底。"""
        mgr = _make_card_manager([Exception("boom")])
        assert await mgr.fail_card("card-1", "出错了") is False
        assert mgr.client.put.await_count == 1  # 不做无谓的终态重试

    @pytest.mark.asyncio
    async def test_fail_card_all_finish_failures_still_true(self):
        """终态与降级均失败：文案已可见，仍返回 True 不重复投递。"""
        mgr = _make_card_manager([
            self._ok_resp(),
            Exception("500 system.busy"),
            Exception("500 system.busy"),
        ])
        assert await mgr.fail_card("card-1", "出错了") is True
        assert mgr.client.put.await_count == 3

    @pytest.mark.asyncio
    async def test_finalize_card_finish_failure_returns_true(self):
        """非流式路径：内容上屏成功 + finish 失败 → 返回 True，降级置状态。"""
        mgr = _make_card_manager([
            self._ok_resp(),
            Exception("500 system.busy"),
            self._ok_resp(),
        ])
        assert await mgr.finalize_card("card-1", "完整内容") is True
        assert mgr.client.put.await_count == 3

    @pytest.mark.asyncio
    async def test_finalize_card_stream_failure_returns_false(self):
        """非流式路径渲染失败：返回 False 让调用方 markdown 兜底。"""
        mgr = _make_card_manager([Exception("boom")])
        assert await mgr.finalize_card("card-1", "完整内容") is False
        assert mgr.client.put.await_count == 1
