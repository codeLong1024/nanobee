"""
channel_http 插件测试 - OpenAI 兼容 HTTP API 通道。

覆盖:
1. _parse_openai_messages：文本模式、多模态模式、空数组
2. _build_sse_chunk：常规增量、终止块
3. _build_json_response：完整响应结构
4. _check_auth：无认证、有效/无效 API Key
5. /v1/models：返回模型列表
6. /v1/models/{model}：返回模型详情、未找到
7. POST /v1/chat/completions（非流式）：正常响应、缺失 messages、内核未就绪
8. POST /v1/chat/completions（流式）：SSE 格式验证
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase

from nanobee.builtin.channel_http.plugin import (
    HTTPChannelPlugin,
    _build_json_response,
    _build_sse_chunk,
    _parse_openai_messages,
)


# =============================================================================
# 辅助函数：创建测试用 mock kernel
# =============================================================================


def _mock_kernel(booted: bool = True) -> MagicMock:
    """创建 mock kernel。"""
    kernel = MagicMock()
    kernel.is_booted = booted
    kernel.config = {
        "agents": {
            "defaults": {
                "model": "test-model",
            },
        },
    }
    return kernel


def _create_plugin(api_key: str | None = None) -> HTTPChannelPlugin:
    """创建测试插件实例，可选配置 API Key。"""
    plugin = HTTPChannelPlugin()
    kernel = _mock_kernel()
    plugin.initialize(kernel)
    # 模拟 get_config 返回 api_key
    original_get_config = plugin.get_config

    def _patched_get_config(key: str, default: Any = None) -> Any:
        if key == "api_key":
            return api_key if api_key is not None else default
        return original_get_config(key, default)

    plugin.get_config = _patched_get_config  # type: ignore[method-assign]
    return plugin


# =============================================================================
# _parse_openai_messages 测试
# =============================================================================


class TestParseOpenAIMessages:
    """测试 _parse_openai_messages 函数。"""

    def test_text_message(self) -> None:
        """单条文本 user 消息。"""
        messages = [
            {"role": "user", "content": "Hello"},
        ]
        text, images = _parse_openai_messages(messages)
        assert text == "Hello"
        assert images == []

    def test_last_user_message_only(self) -> None:
        """多条消息中只提取最后一条 user 消息。"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        text, images = _parse_openai_messages(messages)
        assert text == "Second question"
        assert images == []

    def test_multimodal_content(self) -> None:
        """多模态消息：text + image_url。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                ],
            },
        ]
        text, images = _parse_openai_messages(messages)
        assert text == "What's in this image?"
        assert images == ["https://example.com/img.png"]

    def test_empty_messages(self) -> None:
        """空消息数组。"""
        text, images = _parse_openai_messages([])
        assert text == ""
        assert images == []

    def test_no_user_message(self) -> None:
        """没有 user 消息。"""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "assistant", "content": "Hello!"},
        ]
        text, images = _parse_openai_messages(messages)
        assert text == ""
        assert images == []

    def test_multiple_image_urls(self) -> None:
        """多个 image_url。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare these"},
                    {"type": "image_url", "image_url": {"url": "https://a.com/1.png"}},
                    {"type": "image_url", "image_url": {"url": "https://a.com/2.png"}},
                ],
            },
        ]
        text, images = _parse_openai_messages(messages)
        assert text == "Compare these"
        assert images == ["https://a.com/1.png", "https://a.com/2.png"]

    def test_invalid_message_structure(self) -> None:
        """消息结构异常时容错。"""
        messages = [{"role": "user", "content": 123}]
        text, images = _parse_openai_messages(messages)
        assert text == ""
        assert images == []


# =============================================================================
# _build_sse_chunk 测试
# =============================================================================


class TestBuildSSEChunk:
    """测试 _build_sse_chunk 函数。"""

    def test_content_chunk(self) -> None:
        """常规内容增量块。"""
        result = _build_sse_chunk(
            "Hello", model="test-model", completion_id="cmpl-001",
        )
        assert result.startswith("data: ")
        assert result.endswith("\n\n")
        data = json.loads(result[len("data: "):-2])
        assert data["object"] == "chat.completion.chunk"
        assert data["model"] == "test-model"
        assert data["choices"][0]["delta"]["content"] == "Hello"
        assert data["choices"][0]["finish_reason"] is None

    def test_finish_chunk(self) -> None:
        """终止块（finish_reason=stop）。"""
        result = _build_sse_chunk(
            "", model="test-model", completion_id="cmpl-001", finish_reason="stop",
        )
        data = json.loads(result[len("data: "):-2])
        assert data["choices"][0]["delta"] == {}  # 无 content
        assert data["choices"][0]["finish_reason"] == "stop"


# =============================================================================
# _build_json_response 测试
# =============================================================================


class TestBuildJsonResponse:
    """测试 _build_json_response 函数。"""

    def test_full_response(self) -> None:
        """完整响应结构。"""
        result = _build_json_response(
            "Hello!", model="test-model", completion_id="cmpl-001",
        )
        assert result["id"] == "cmpl-001"
        assert result["object"] == "chat.completion"
        assert result["model"] == "test-model"
        assert result["choices"][0]["message"]["role"] == "assistant"
        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert "usage" in result


# =============================================================================
# _check_auth 测试
# =============================================================================


class TestCheckAuth:
    """测试 _check_auth 方法。"""

    def test_no_auth_configured(self) -> None:
        """未配置 API Key 时允许所有请求。"""
        plugin = _create_plugin(api_key=None)
        mock_request = MagicMock()
        mock_request.headers = {}
        assert plugin._check_auth(mock_request) is True

    def test_valid_key(self) -> None:
        """有效的 API Key。"""
        plugin = _create_plugin(api_key="sk-test-key")
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer sk-test-key"}
        assert plugin._check_auth(mock_request) is True

    def test_invalid_key(self) -> None:
        """无效的 API Key。"""
        plugin = _create_plugin(api_key="sk-test-key")
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer sk-wrong-key"}
        assert plugin._check_auth(mock_request) is False

    def test_missing_header(self) -> None:
        """缺少 Authorization 头。"""
        plugin = _create_plugin(api_key="sk-test-key")
        mock_request = MagicMock()
        mock_request.headers = {}
        assert plugin._check_auth(mock_request) is False

    def test_wrong_auth_type(self) -> None:
        """非 Bearer 认证类型。"""
        plugin = _create_plugin(api_key="sk-test-key")
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        assert plugin._check_auth(mock_request) is False


# =============================================================================
# HTTP 端点集成测试（使用 aiohttp TestClient）
# =============================================================================


class TestHTTPEndpoints(AioHTTPTestCase):
    """在 aiohttp TestClient 中测试 HTTP 端点。"""

    async def get_application(self) -> Any:
        """创建测试用 aiohttp 应用。"""
        from aiohttp import web

        # 创建插件实例并初始化
        self.plugin = _create_plugin()
        kernel = self.plugin.kernel

        # Mock handle_message
        self._mock_response = AsyncMock()
        self._mock_response.content = "Test response"
        kernel.handle_message = AsyncMock(return_value=self._mock_response)

        # 将插件的方法暴露为路由
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.plugin._handle_completions)
        app.router.add_get("/v1/models", self.plugin._handle_models)
        app.router.add_get("/v1/models/{model}", self.plugin._handle_model_detail)
        return app

    async def test_models_list(self) -> None:
        """GET /v1/models 返回模型列表。"""
        resp = await self.client.get("/v1/models")
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == "test-model"

    async def test_model_detail_found(self) -> None:
        """GET /v1/models/test-model 返回模型详情。"""
        resp = await self.client.get("/v1/models/test-model")
        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "test-model"
        assert data["object"] == "model"

    async def test_model_detail_not_found(self) -> None:
        """GET /v1/models/nonexistent 返回 404。"""
        resp = await self.client.get("/v1/models/nonexistent")
        assert resp.status == 404
        data = await resp.json()
        assert "not found" in data["error"]["message"]

    async def test_completions_json_basic(self) -> None:
        """POST /v1/chat/completions (stream=false) 返回 JSON。"""
        payload = {
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert "Test response" in data["choices"][0]["message"]["content"]
        # 验证 handle_message 被正确调用
        self.plugin.kernel.handle_message.assert_awaited_once()

    async def test_completions_missing_messages(self) -> None:
        """POST /v1/chat/completions 缺少 messages 返回 400。"""
        resp = await self.client.post("/v1/chat/completions", json={})
        assert resp.status == 400
        data = await resp.json()
        assert "Missing required field" in data["error"]["message"]

    async def test_completions_empty_messages(self) -> None:
        """POST /v1/chat/completions 空 messages 返回 400。"""
        resp = await self.client.post("/v1/chat/completions", json={"messages": []})
        assert resp.status == 400
        data = await resp.json()
        assert "Missing required field" in data["error"]["message"]

    async def test_completions_invalid_json(self) -> None:
        """POST /v1/chat/completions 无效 JSON 返回 400。"""
        resp = await self.client.post(
            "/v1/chat/completions",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "Invalid JSON" in data["error"]["message"]

    async def test_completions_kernel_not_booted(self) -> None:
        """内核未启动时返回 503。"""
        self.plugin.kernel.is_booted = False
        payload = {
            "messages": [{"role": "user", "content": "Hello"}],
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        assert resp.status == 503
        data = await resp.json()
        assert "Kernel not ready" in data["error"]["message"]

    async def test_completions_streaming(self) -> None:
        """POST /v1/chat/completions (stream=true) 返回 SSE 流。"""
        # 覆盖 handle_message 以调用 on_stream 回调
        async def _streaming_handle(message, context_id="default", *, media=None, on_stream=None, on_stream_end=None, sender_id="user"):
            if on_stream:
                await on_stream("Hello")
                await on_stream(" world")
            if on_stream_end:
                await on_stream_end(resuming=False)
            return self._mock_response

        self.plugin.kernel.handle_message = _streaming_handle

        payload = {
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"

        # 读取 SSE 事件
        text = await resp.text()
        events = text.strip().split("\n\n")

        # 第一个事件应该包含 role 标记
        first_data = events[0]
        assert first_data.startswith("data: ")
        first_event = json.loads(first_data[len("data: "):])
        assert first_event["choices"][0]["delta"]["role"] == "assistant"

        # 中间的内容事件
        content_events = events[1:-2]  # 排除 role 事件、finish 事件和 [DONE]
        combined = ""
        for evt in content_events:
            parsed = json.loads(evt[len("data: "):])
            delta = parsed["choices"][0]["delta"]
            if "content" in delta:
                combined += delta["content"]
        assert combined == "Hello world"

        # 倒数第二个事件应该是 finish_reason
        second_last = events[-2]
        assert second_last.startswith("data: ")
        finish_event = json.loads(second_last[len("data: "):])
        assert finish_event["choices"][0]["finish_reason"] == "stop"

        # 最后一个应该是 [DONE]
        assert events[-1] == "data: [DONE]"


# =============================================================================
# 快速单元测试
# =============================================================================


def test_parse_last_user_message_text_only() -> None:
    """只提取最后一条 user 消息的文本。"""
    text, _ = _parse_openai_messages([
        {"role": "system", "content": "ignore"},
        {"role": "user", "content": "keep me"},
    ])
    assert text == "keep me"


def test_parse_empty_user_message_returns_empty() -> None:
    """没有 user 消息返回空字符串。"""
    text, _ = _parse_openai_messages([{"role": "assistant", "content": "hi"}])
    assert text == ""


# =============================================================================
# 认证集成测试（使用独立 aiohttp TestClient）
# =============================================================================


class TestHTTPAuthEndpoints(AioHTTPTestCase):
    """测试 API Key 认证。"""

    async def get_application(self) -> Any:
        from aiohttp import web

        self.plugin = _create_plugin(api_key="sk-secret")
        kernel = self.plugin.kernel
        self._mock_response = AsyncMock()
        self._mock_response.content = "Test"
        kernel.handle_message = AsyncMock(return_value=self._mock_response)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.plugin._handle_completions)
        return app

    async def test_auth_required_no_key(self) -> None:
        """未提供 API Key 返回 401。"""
        resp = await self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status == 401
        data = await resp.json()
        assert "Unauthorized" in data["error"]["message"]

    async def test_auth_required_wrong_key(self) -> None:
        """错误 API Key 返回 401。"""
        resp = await self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer sk-wrong"},
        )
        assert resp.status == 401

    async def test_auth_required_valid_key(self) -> None:
        """有效 API Key 请求正常处理。"""
        resp = await self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer sk-secret"},
        )
        assert resp.status == 200
