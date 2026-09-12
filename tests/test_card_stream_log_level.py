"""CardManager 流式接口响应的日志分级契约。

背景：``card_manager.stream_content`` 在流式回复期间会被逐 chunk 调用，
若成功响应体留在 DEBUG，一次回复会产生 O(内容长度) 条长日志（"DEBUG 一开
就满屏"）。分级契约为「高频明细进 TRACE，DEBUG 只留状态与异常」：

- 200 响应 → ``TRACE``（DEBUG sink 下不可见）
- 非 200 响应 → ``DEBUG``（异常才需要在默认级别可见，且需携带 body 便于定位）
- 逐 chunk 明细（完整内容）→ ``TRACE``

需完整回放时把 ``logging.level`` 设为 ``TRACE``（loguru 原生级别，文件
sink 直接透传）。

本文件为契约锁定型用例（护栏，写下即绿）：行为已在
``085fbd2 refactor(channel_dingtalk): 流式回复日志降噪`` 落地，
此处冻结语义，防止后续改动无声回退为逐 chunk DEBUG 刷屏。
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from nanobee.builtin.channel_dingtalk.card_manager import CardManager


def _capture_debug_logs() -> tuple[io.StringIO, "callable[[], None]"]:
    """挂一个 DEBUG 级 sink 捕获日志，返回 (缓冲, 清理函数)。"""
    buf = io.StringIO()
    handler_id = logger.add(buf, level="DEBUG", format="{level} | {message}")
    return buf, lambda: logger.remove(handler_id)


def _make_manager(status_code: int, body: str) -> CardManager:
    """构造零网络依赖的 CardManager 替身（put 返回指定状态码）。"""
    client = MagicMock()
    client.api_url = "https://api.dingtalk.com/v1.0"
    client.ensure_async_client = AsyncMock(return_value=client)
    client.get_headers_async = AsyncMock(return_value={})
    client.check_response = AsyncMock()
    client.put = AsyncMock(return_value=MagicMock(status_code=status_code, text=body))
    return CardManager(client)


class TestStreamRespLogLevel:
    """流式响应日志分级契约（200 → TRACE / 非 200 → DEBUG）。"""

    @pytest.mark.asyncio
    async def test_success_resp_not_logged_at_debug(self):
        """200 响应属高频明细，必须走 TRACE：DEBUG sink 下不得出现。

        连推 3 个 chunk 验证「逐 chunk 不刷屏」；同时锁定 finish_streaming
        的 body 摘要（含完整 msgContent）也不落 DEBUG。
        """
        buf, cleanup = _capture_debug_logs()
        try:
            mgr = _make_manager(200, '{"result":true}')
            for i in range(3):  # 模拟 3 个流式 chunk
                await mgr.stream_content("card-1", f"第 {i} 段累积内容……")
            await mgr.finish_streaming("card-1", "最终完整回复")
            logged = buf.getvalue()
            assert "stream_content resp" not in logged, (
                "200 响应不得出现在 DEBUG（会随逐 chunk 推送刷屏），应落在 TRACE"
            )
            assert "finish_streaming body" not in logged, (
                "finish_streaming 的 body（含完整 msgContent）不得出现在 DEBUG"
            )
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_error_resp_logged_at_debug(self):
        """非 200 响应属异常，必须保留在 DEBUG，且携带 status 与 body。

        回归锚点：降噪不得把异常也埋进 TRACE —— QpsLimit(403)/500 等
        故障是默认级别下唯一需要看到的流式信息。
        """
        buf, cleanup = _capture_debug_logs()
        try:
            mgr = _make_manager(500, "500 system.busy")
            mgr.client.check_response = AsyncMock(side_effect=AssertionError("500"))
            with pytest.raises(AssertionError):
                await mgr.stream_content("card-1", "内容")
            logged = buf.getvalue()
            assert "status=500" in logged, "非 200 响应必须在 DEBUG 可见"
            assert "system.busy" in logged, "DEBUG 日志需携带响应 body 便于定位"
        finally:
            cleanup()
