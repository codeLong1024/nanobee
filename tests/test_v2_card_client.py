"""v2 结构化卡片 REST 客户端测试。

覆盖场景：
1. 创建契约：/card/instances 请求体（v2 模板+初始变量+STREAM）与投放请求体
   （单聊/群聊两种 openSpaceId 模式）
2. 两步更新契约：先 streaming（key=content）后 instances；终帧 streaming 置空
3. 写入串行化：并发 push 按序执行
4. 终态拒迟到帧：终态后 push 返回 False 且不发起请求
5. QpsLimit 退避重试：403 QpsLimit 后重试一次成功
6. 失败降级：服务端拒绝投放时抛 RuntimeError
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nanobee.builtin.channel_dingtalk.card_client import DingTalkCardClient
from nanobee.builtin.channel_dingtalk.v2_card_client import DingTalkV2CardClient
from nanobee.builtin.channel_dingtalk.v2_card_renderer import V2CardDraft


class _FakeHttp:
    """最小 HTTP 替身：记录请求并委托 responder 产出响应。"""

    def __init__(self, responder: Any, log: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._responder = responder
        self.log = log

    async def request(self, method: str, url: str, headers: Any = None,
                      json: Any = None) -> _FakeResponse:
        self.log.append((method, url, json))
        return self._responder(method, url, json)


class _FakeCardClient(DingTalkCardClient):
    """替身：注入假 HTTP 层，记录全部请求。"""

    def __init__(self, responder: Any) -> None:
        super().__init__()
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self._fake_http = _FakeHttp(responder, self.requests)

    async def ensure_async_client(self) -> httpx.AsyncClient:
        return self._fake_http  # type: ignore[return-value]

    async def get_headers_async(self) -> dict[str, str]:
        return {"x-acs-dingtalk-access-token": "fake"}

    async def check_response(self, resp: Any, operation: str = "API call") -> None:
        if getattr(resp, "force_error", False):
            raise httpx.HTTPStatusError(
                f"Client error: {resp.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


class _FakeResponse:
    """最小响应替身。"""

    def __init__(self, payload: dict[str, Any] | None = None, *, force_error: bool = False,
                 status_code: int = 200, text: str = "") -> None:
        self._payload = payload or {}
        self.force_error = force_error
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.force_error:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


@pytest.fixture
def ok_responder():
    """默认成功响应：按请求 URL 返回。"""
    def respond(method: str, url: str, body: dict[str, Any]) -> _FakeResponse:
        if url.endswith("/card/instances/deliver"):
            return _FakeResponse({"success": True})
        return _FakeResponse({"result": True, "success": True})
    return respond


def _make_client(responder: Any, target: dict[str, str] | None = None) -> DingTalkV2CardClient:
    fake = _FakeCardClient(responder)
    return DingTalkV2CardClient(
        fake, "robot-1", target or {"receiverUserId": "u1"},
        out_track_id="card_test_1",
    )


class TestCreate:
    """创建契约。"""

    @pytest.mark.asyncio
    async def test_create_request_bodies(self, ok_responder) -> None:
        """创建请求体与官方 create 契约对齐。"""
        client = _make_client(ok_responder)
        fake = client._client
        await client.create(V2CardDraft().create_param_map())

        create_method, create_url, create_body = fake.requests[0]
        assert (create_method, create_url) == ("POST", "https://api.dingtalk.com/v1.0/card/instances")
        assert create_body["cardTemplateId"].endswith(".schema")
        assert create_body["outTrackId"] == "card_test_1"
        assert create_body["callbackType"] == "STREAM"
        assert create_body["cardData"]["cardParamMap"]["flowStatus"] == "1"

        deliver_method, deliver_url, deliver_body = fake.requests[1]
        assert deliver_method == "POST"
        assert deliver_url.endswith("/card/instances/deliver")
        assert deliver_body["openSpaceId"] == "dtv1.card//IM_ROBOT.u1"
        assert deliver_body["imRobotOpenDeliverModel"]["robotCode"] == "robot-1"

    @pytest.mark.asyncio
    async def test_create_group_target(self, ok_responder) -> None:
        """群聊目标：openSpaceId 用 IM_GROUP。"""
        client = _make_client(ok_responder, target={"openConversationId": "cidX"})
        await client.create(V2CardDraft().create_param_map())
        _, _, deliver_body = client._client.requests[1]
        assert deliver_body["openSpaceId"] == "dtv1.card//IM_GROUP.cidX"

    @pytest.mark.asyncio
    async def test_delivery_rejected_raises(self) -> None:
        """投放被拒绝（success=false）：抛 RuntimeError。"""
        def responder(method: str, url: str, body: dict[str, Any]) -> _FakeResponse:
            if url.endswith("/card/instances/deliver"):
                return _FakeResponse({"success": False, "result": []})
            return _FakeResponse({})

        client = _make_client(responder)
        with pytest.raises(RuntimeError, match="投放被拒绝"):
            await client.create(V2CardDraft().create_param_map())


class TestPush:
    """两步更新契约。"""

    @pytest.mark.asyncio
    async def test_two_step_update_order(self, ok_responder) -> None:
        """每帧先 streaming 后 instances；终帧 streaming 置空且 isFinalize=True。"""
        client = _make_client(ok_responder)
        draft = V2CardDraft(show_metadata=False)
        draft.set_answer("正文")

        frame = draft.frame()
        assert await client.push(frame) is True
        _, url1, body1 = client._client.requests[0]
        assert url1.endswith("/card/streaming")
        assert body1["key"] == "content"
        assert body1["content"] == "正文"
        assert body1["isFinalize"] is False

        _, url2, body2 = client._client.requests[1]
        assert url2.endswith("/card/instances")
        param_map = body2["cardData"]["cardParamMap"]
        assert json.loads(param_map["blockList"]) == []
        assert param_map["flowStatus"] == "2"

        final_frame = draft.frame(finished=True)
        assert await client.push(final_frame) is True
        _, _, body3 = client._client.requests[2]
        assert body3["content"] == ""
        assert body3["isFinalize"] is True
        _, _, body4 = client._client.requests[3]
        assert body4["cardData"]["cardParamMap"]["flowStatus"] == "3"

    @pytest.mark.asyncio
    async def test_terminal_frame_tolerates_streaming_failure(self) -> None:
        """终态帧 streaming 关闭失败仍提交 instances 终态（官方 0.8.27 语义）。

        非终态帧 streaming 失败照常抛出；终态帧若第一步失败就放弃第二步，
        卡片会永久停留在转圈态——这是官方注释确认的最影响体验的失败路径。
        """
        calls: dict[str, int] = {"instances": 0}

        def responder(method: str, url: str, body: dict[str, Any]) -> _FakeResponse:
            if url.endswith("/card/streaming"):
                return _FakeResponse(force_error=True)
            if url.endswith("/card/instances"):
                calls["instances"] += 1
            return _FakeResponse({"success": True})

        client = _make_client(responder)
        draft = V2CardDraft(show_metadata=False)
        draft.set_answer("半截进度")

        # 非终态帧：streaming 失败照常抛出
        with pytest.raises(httpx.HTTPStatusError):
            await client.push(draft.frame())

        # 终态帧：streaming 失败被容忍，instances 终态仍提交
        assert await client.push(draft.frame(finished=True)) is True
        assert calls["instances"] == 1
        assert client._terminal is True

    @pytest.mark.asyncio
    async def test_terminal_rejects_late_frames(self, ok_responder) -> None:
        """终态后迟到帧：push 返回 False，不发起任何请求。"""
        client = _make_client(ok_responder)
        draft = V2CardDraft(show_metadata=False)
        await client.push(draft.frame(finished=True))
        sent_before = len(client._client.requests)

        late = draft.frame()
        assert await client.push(late) is False
        assert len(client._client.requests) == sent_before

    @pytest.mark.asyncio
    async def test_concurrent_push_serialized(self, ok_responder) -> None:
        """并发 push：同卡片写入按序串行（无交叉）。"""
        client = _make_client(ok_responder)
        draft = V2CardDraft(show_metadata=False)

        async def push_one(tag: str) -> None:
            draft.set_answer(f"chunk-{tag}")
            await client.push(draft.frame())

        await asyncio.gather(*(push_one(i) for i in range(4)))
        streaming = [b for m, u, b in client._client.requests if u.endswith("/card/streaming")]
        assert len(streaming) == 4
        # 锁保证四个帧各自完整写入（streaming 与 instances 严格交替）
        urls = [u for _, u, _ in client._client.requests]
        expected = ["streaming", "instances"] * 4
        assert [u.rsplit("/", 1)[-1] for u in urls] == expected


class TestQpsRetry:
    """QpsLimit 退避。"""

    @pytest.mark.asyncio
    async def test_qps_limit_retry_once(self) -> None:
        """403 QpsLimit：退避后重试一次成功。"""
        calls = {"stream": 0}

        def responder(method: str, url: str, body: dict[str, Any]) -> _FakeResponse:
            if url.endswith("/card/streaming"):
                calls["stream"] += 1
                if calls["stream"] == 1:
                    return _FakeResponse(status_code=403, text="QpsLimit")
            return _FakeResponse({"success": True})

        client = _make_client(responder)
        draft = V2CardDraft(show_metadata=False)
        assert await client.push(draft.frame()) is True
        assert calls["stream"] == 2
