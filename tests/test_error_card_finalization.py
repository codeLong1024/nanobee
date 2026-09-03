"""错误卡片完结的唯一出口契约测试。

背景：LLM 出错后钉钉流式卡片"消失"。根因是错误被两个框架层各发一次——
runner 经 ``on_stream_end`` 把错误卡片终态化（FINISHED），loop 又通过
``turn_internal_error`` 系统通知走 ``fail_card``，同一张卡片被二次终态化；
而钉钉 FAILED 态不渲染 /card/streaming 内容 → 卡片消失。

契约（本测试锁定的目标行为）：
1. runner 的 ``on_stream_end`` 只负责"流结束"传输信号，不携带 error、错误时
   不触发——由 tests/test_runner_error_streaming.py 锁定（强断言：未被调用）。
2. 错误完结只有一个出口：``loop → _deliver_system_notification → fail_card``。
3. ``fail_card`` 两步式：先 ``stream_content`` 渲染错误文案，再置 FINISHED
   （不得用 FAILED 态），保证钉钉卡片 UI（只渲染 /card/streaming）能显示错误文案。
4. 流式中途出错（buffer 已有半截内容）时，``fail_card`` 保留半截 + 追加失败提示，
   不清空用户已看到的进度。
5. ``fail_card`` 两步均失败时，通道回落 markdown 文本兜底，用户不零感知。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.builtin.channel_dingtalk.card_manager import CardManager


# ============================================================
# 契约 2：fail_card 两步式（先渲染文案，再置 FINISHED）
# ============================================================


@pytest.mark.asyncio
async def test_fail_card_uses_finished_status_not_failed():
    """fail_card 用 FINISHED 态展示错误文案，而非 FAILED 态。

    依据钉钉官方 SDK：flowStatus=FAILED 的卡片不携带 msgContent（仅
    msgTitle/logo），客户端对 FAILED 态会清空内容（"一闪而过"）。要让错误
    文案可见，必须复用 FINISHED 态（与正常完成一致），仅内容换成错误文案。
    故 fail_card 应：
    1. /card/streaming 推错误文案（isFinalize=True）
    2. /card/instances 置 flowStatus=FINISHED（"3"），不得出现 FAILED（"5"）
    """
    _resp = MagicMock()
    _resp.raise_for_status = MagicMock()

    _inner = MagicMock()
    _inner.put = AsyncMock(return_value=_resp)
    _inner.check_response = AsyncMock()
    _inner.get_headers_async = AsyncMock(return_value={})
    _inner.api_url = "https://api.dingtalk.com/v1.0"

    client = MagicMock()
    client.ensure_async_client = AsyncMock(return_value=_inner)
    client.get_headers_async = AsyncMock(return_value={})
    client.check_response = AsyncMock()

    mgr = CardManager(client)

    await mgr.fail_card("card-1", "处理失败: timeout")

    put_urls = [c.args[0] for c in _inner.put.await_args_list]
    # 第一步：/card/streaming 推文案
    assert any("/card/streaming" in u for u in put_urls), "fail_card 必须先经 /card/streaming 渲染文案"
    streaming_body = next(
        c.kwargs["json"] for c in _inner.put.await_args_list
        if "/card/streaming" in c.args[0]
    )
    assert streaming_body["isFinalize"] is True
    assert "处理失败" in streaming_body["content"]

    # 第二步：/card/instances 置 FINISHED（复用 finish_streaming），绝不用 FAILED
    instances_bodies = [
        c.kwargs["json"] for c in _inner.put.await_args_list
        if "/card/instances" in c.args[0]
    ]
    assert instances_bodies, "fail_card 必须经 /card/instances 置终态"
    flow_statuses = [
        b["cardData"]["cardParamMap"]["flowStatus"] for b in instances_bodies
    ]
    assert all(s == "3" for s in flow_statuses), (
        f"fail_card 应用 FINISHED(3) 态而非 FAILED(5) 态，实际 flowStatus={flow_statuses}"
    )

    # 顺序：/card/streaming 先于 /card/instances
    streaming_idx = next(i for i, u in enumerate(put_urls) if "/card/streaming" in u)
    instances_idx = next(i for i, u in enumerate(put_urls) if "/card/instances" in u)
    assert streaming_idx < instances_idx, "渲染必须先于 FINISHED 终态"


# ============================================================
# 契约 3：流式中途出错（buffer 有半截）保留进度 + 追加失败提示
# ============================================================


@pytest.mark.asyncio
async def test_error_notification_preserves_partial_stream_buffer():
    """类型 B/C：卡片已有半截流式内容时，fail_card 保留半截 + 追加失败提示。

    出错完结不得清空用户已看到的进度；拼接顺序为"半截内容 + 分割线 + 失败提示"。
    """
    from types import SimpleNamespace

    from nanobee.builtin.channel_dingtalk.channel import DingTalkChannelPlugin
    from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
    from nanobee.channel.message import OutboundMessage

    plugin = DingTalkChannelPlugin.__new__(DingTalkChannelPlugin)
    plugin.__init__(metadata=SimpleNamespace(name="channel_dingtalk"))
    plugin.logger = MagicMock()
    plugin.dingtalk_config = DingTalkConfig(streaming=False)
    plugin.name = "channel_dingtalk"

    card_manager = MagicMock()
    card_manager.fail_card = AsyncMock()
    plugin.card_manager = card_manager

    # 模拟已有半截流式内容
    sender = MagicMock()
    sender.take_stream_buffer = MagicMock(return_value="我来查一下数据")
    plugin.sender = sender

    response = OutboundMessage(
        channel="channel_dingtalk",
        chat_id="conv-test",
        content="抱歉，处理消息时发生内部错误。",
        metadata={"notification_type": "system", "severity": "error"},
    )

    await plugin._deliver_system_notification(
        response, "conv-test", "card-err-2", "msg-err-2",
        response.metadata,
    )

    # 完结文案 = 半截进度 + 分割线 + 失败提示
    expected = "我来查一下数据\n\n---\n⚠️ 抱歉，处理消息时发生内部错误。"
    card_manager.fail_card.assert_awaited_once_with("card-err-2", expected)


# ============================================================
# 契约 4：错误 turn 唯一出口是 _deliver_system_notification 的 fail_card
# ============================================================


@pytest.mark.asyncio
async def test_error_turn_has_single_fail_card_exit():
    """错误完结的唯一出口是 fail_card，不得再有 on_stream_end 终态化。

    验证：channel 侧 _deliver_system_notification 的 error 分支，
    对同一 card_id 只调用 fail_card 一次，且不再走 finalize_card_with_notification。
    """
    from types import SimpleNamespace

    from nanobee.builtin.channel_dingtalk.channel import DingTalkChannelPlugin
    from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
    from nanobee.channel.message import OutboundMessage

    plugin = DingTalkChannelPlugin.__new__(DingTalkChannelPlugin)
    plugin.__init__(metadata=SimpleNamespace(name="channel_dingtalk"))
    plugin.logger = MagicMock()
    plugin.dingtalk_config = DingTalkConfig(streaming=False)
    plugin.name = "channel_dingtalk"

    card_manager = MagicMock()
    card_manager.fail_card = AsyncMock()
    plugin.card_manager = card_manager

    sender = MagicMock()
    sender.finalize_card_with_notification = AsyncMock()
    sender.is_card_handled_by_streaming = MagicMock(return_value=False)
    sender.take_stream_buffer = MagicMock(return_value="")
    plugin.sender = sender

    response = OutboundMessage(
        channel="channel_dingtalk",
        chat_id="conv-test",
        content="抱歉，处理消息时发生内部错误。",
        metadata={"notification_type": "system", "severity": "error"},
    )

    await plugin._deliver_system_notification(
        response, "conv-test", "card-err-1", "msg-err-1",
        response.metadata,
    )

    # 唯一出口：fail_card 一次
    card_manager.fail_card.assert_awaited_once_with("card-err-1", "抱歉，处理消息时发生内部错误。")
    # 不得再走流式终态化（finalize_card_with_notification）
    sender.finalize_card_with_notification.assert_not_called()


# ============================================================
# 契约 5：fail_card 两步均失败时回落 markdown 兜底
# ============================================================


@pytest.mark.asyncio
async def test_fail_card_failure_falls_back_to_text_response():
    """fail_card 两步均失败（返回 False）时，回落 markdown 文本兜底。

    卡片渲染链路整体故障时，用户仍能通过普通文本消息看到错误提示，
    避免卡片永久停在 INPUTING 且用户零感知。
    """
    from types import SimpleNamespace

    from nanobee.builtin.channel_dingtalk.channel import DingTalkChannelPlugin
    from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
    from nanobee.channel.message import OutboundMessage

    plugin = DingTalkChannelPlugin.__new__(DingTalkChannelPlugin)
    plugin.__init__(metadata=SimpleNamespace(name="channel_dingtalk"))
    plugin.logger = MagicMock()
    plugin.dingtalk_config = DingTalkConfig(streaming=False)
    plugin.name = "channel_dingtalk"

    card_manager = MagicMock()
    card_manager.fail_card = AsyncMock(return_value=False)
    plugin.card_manager = card_manager

    sender = MagicMock()
    sender.send = AsyncMock()
    sender.take_stream_buffer = MagicMock(return_value="")
    plugin.sender = sender

    response = OutboundMessage(
        channel="channel_dingtalk",
        chat_id="conv-test",
        content="抱歉，处理消息时发生内部错误。",
        metadata={"notification_type": "system", "severity": "error"},
    )

    await plugin._deliver_system_notification(
        response, "conv-test", "card-err-3", "msg-err-3",
        response.metadata,
    )

    # fail_card 失败 → 回落 markdown 兜底投递
    card_manager.fail_card.assert_awaited_once()
    sender.send.assert_awaited_once()
    assert "内部错误" in sender.send.await_args[0][0].content
