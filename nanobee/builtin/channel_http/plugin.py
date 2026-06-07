"""
OpenAI 兼容 HTTP API 通道插件。

将 nanobee 暴露为 OpenAI 兼容的 HTTP API，支持 LobeChat 等第三方客户端连接。

启动一个 aiohttp 服务器，提供以下端点：
- POST /v1/chat/completions — 核心对话（支持流式和非流式）
- GET  /v1/models           — 返回可用模型列表
- GET  /v1/models/{model}   — 返回单个模型详情

配置（在 nanobee.yaml 中）：
```yaml
plugins:
  channel_http:
    host: "127.0.0.1"       # 监听地址，默认 127.0.0.1
    port: 8080              # 监听端口，默认 8080
    api_key: "sk-xxx"       # API Key（可选，不配置时无需认证）
```

4 个语义鸿沟的桥接策略：
1. 消息格式差异：提取 messages[] 中最后一条 user 消息为文本
2. 历史冲突：不注入 LobeChat 历史，由 ContextManager 自行管理
3. 工具调用：忽略客户端 tools，使用 nanobee 的 ToolRegistry
4. SSE 格式：on_stream delta → OpenAI SSE 事件
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from aiohttp import web

from nanobee.channel.base import ChannelPlugin
from nanobee.channel.message import OutboundMessage, StreamingDelta

from nanobee.utils.logger import logger



# =============================================================================
# OpenAI SSE 格式常量
# =============================================================================

_CONTENT_TYPE_SSE = "text/event-stream"
_DONE_MARKER = "[DONE]"

# OpenAI 兼容角色名称
_ROLE_ASSISTANT = "assistant"
_ROLE_USER = "user"
_ROLE_SYSTEM = "system"

# 默认 completion ID 前缀
_COMPLETION_ID_PREFIX = "chatcmpl-"


# =============================================================================
# 辅助函数
# =============================================================================

def _make_completion_id() -> str:
    """生成 OpenAI 风格的 completion ID。"""
    return f"{_COMPLETION_ID_PREFIX}{uuid.uuid4().hex[:12]}"


def _make_timestamp() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return int(time.time())


def _build_sse_chunk(
    delta_text: str,
    *,
    model: str,
    completion_id: str,
    finish_reason: str | None = None,
) -> str:
    """构建 OpenAI SSE 格式的流式数据块。

    Args:
        delta_text: 文本增量
        model: 模型名称
        completion_id: completion ID
        finish_reason: 结束原因（stop/length/null）

    Returns:
        SSE data 行字符串（含 ``data: `` 前缀和末尾双换行）
    """
    delta: dict[str, Any] = {"content": delta_text}
    if finish_reason:
        delta = {}  # 最后一块只有 finish_reason，不重复 content

    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": _make_timestamp(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _build_json_response(
    content: str,
    *,
    model: str,
    completion_id: str | None = None,
) -> dict[str, Any]:
    """构建非流式 JSON 响应体。

    Args:
        content: assistant 回复文本
        model: 模型名称
        completion_id: completion ID（可选）

    Returns:
        OpenAI 兼容的 JSON 响应字典
    """
    cid = completion_id or _make_completion_id()
    return {
        "id": cid,
        "object": "chat.completion",
        "created": _make_timestamp(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": _ROLE_ASSISTANT,
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _parse_openai_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """从 OpenAI messages[] 数组中提取最后一条 user 消息。

    策略：
    - 遍历 messages，找到最后一条 ``role=user`` 的消息
    - 如果 content 是字符串，直接返回
    - 如果 content 是数组（多模态），拼接 text 部分，收集 image_url
    - 忽略 system/assistant/tool 消息（由 ContextManager 管理历史）

    Args:
        messages: OpenAI 格式的消息数组

    Returns:
        (用户文本, 图片 URL 列表)
    """
    text_parts: list[str] = []
    image_urls: list[str] = []

    # 从后往前找最后一条 user 消息
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role != _ROLE_USER:
            continue

        content = msg.get("content", "")
        if isinstance(content, str):
            text_parts.append(content)
            break  # 找到最后一条 user 消息即可
        elif isinstance(content, list):
            # 多模态内容：text + image_url
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                if part_type == "text":
                    text_parts.append(part.get("text", ""))
                elif part_type == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url:
                        image_urls.append(url)
            break  # 找到最后一条 user 消息即可

    text = " ".join(p.strip() for p in text_parts if p.strip()).strip()
    return text, image_urls


# =============================================================================
# HTTP 通道插件
# =============================================================================

class HTTPChannelPlugin(ChannelPlugin):
    """HTTP 通道插件 — OpenAI 兼容 API。

    启动一个 aiohttp 服务器，将 nanobee 暴露为 OpenAI 兼容的 HTTP API。
    """

    name = "channel_http"
    version = "0.1.0"
    display_name = "HTTP API"
    supports_streaming = True

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._model: str = "default"

    # ====== 生命周期 ======

    def initialize(self, kernel: Any) -> None:
        """初始化插件时读取配置。"""
        super().initialize(kernel)
        # 从配置读取模型名称（兼容 Config 对象和 dict）
        cfg = kernel.config
        if hasattr(cfg, "agents"):
            self._model = cfg.agents.defaults.model or "default"
        else:
            agents = cfg.get("agents", {})
            defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
            self._model = defaults.get("model", "default") if isinstance(defaults, dict) else "default"
        logger.info("HTTP 通道模型: {}", self._model)

    async def start(self) -> None:
        """启动 aiohttp HTTP 服务器。"""
        host = self.get_config("host", "127.0.0.1")
        port = int(self.get_config("port", 8080))

        self._app = web.Application()
        self._app.router.add_post("/v1/chat/completions", self._handle_completions)
        self._app.router.add_get("/v1/models", self._handle_models)
        self._app.router.add_get("/v1/models/{model}", self._handle_model_detail)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

        logger.info(
            "HTTP 通道已启动: http://%s:%s (model=%s, auth=%s)",
            host, port, self._model,
            "enabled" if self.get_config("api_key") else "disabled",
        )

    async def stop(self) -> None:
        """停止 aiohttp HTTP 服务器。"""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        self._site = None
        logger.info("HTTP 通道已停止")

    # ====== ChannelPlugin 接口（请求-响应模式，不使用 push 发送） ======

    async def send(self, message: OutboundMessage, context_id: str = "default") -> None:
        """HTTP 通道不使用 send（响应在 handler 中直接返回）。"""
        logger.debug("HTTP 通道 send() 被调用（无操作）: {}", context_id)

    async def send_delta(self, delta: StreamingDelta, context_id: str = "default") -> None:
        """HTTP 通道不使用 send_delta（流式由 SSE 处理）。"""
        logger.debug("HTTP 通道 send_delta() 被调用（无操作）: {}", context_id)

    async def _process_incoming(self, message: Any, context_manager: Any) -> list[OutboundMessage]:
        """HTTP 通道不使用 _process_incoming（由 handler 直接处理）。"""
        logger.warning("HTTP 通道 _process_incoming 被调用（不应发生）")
        return []

    # ====== 认证 ======

    def _check_auth(self, request: web.Request) -> bool:
        """检查请求的 Authorization 头。

        如果未配置 api_key，允许所有请求。
        如果配置了 api_key，要求 ``Authorization: Bearer <api_key>``。

        Args:
            request: aiohttp 请求对象

        Returns:
            True 表示认证通过或未配置认证
        """
        configured_key = self.get_config("api_key")
        if not configured_key:
            return True  # 未配置 API Key，不限制

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[len("Bearer "):].strip()
        return token == configured_key

    # ====== Handler: POST /v1/chat/completions ======

    async def _handle_completions(self, request: web.Request) -> web.StreamResponse:
        """处理 POST /v1/chat/completions。

        支持 ``stream=true``（SSE 流式）和 ``stream=false``（JSON 响应）。

        Semantic gaps bridged:
        1. 消息格式：从 OpenAI messages[] 中提取 user 文本
        2. 历史冲突：不注入客户端历史，由 ContextManager 管理
        3. 工具调用：忽略客户端 tools，使用 nanobee 自身的 ToolRegistry
        4. SSE 格式：on_stream delta 封装为 OpenAI SSE 事件
        """
        # ---- 认证 ----
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "auth_error", "code": 401}},
                status=401,
            )

        # ---- 解析请求体 ----
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error", "code": 400}},
                status=400,
            )

        messages = body.get("messages", [])
        if not messages:
            return web.json_response(
                {"error": {"message": "Missing required field: messages", "type": "invalid_request_error", "code": 400}},
                status=400,
            )

        stream = bool(body.get("stream", False))
        conversation_id = body.get("conversation_id") or body.get("thread_id") or str(uuid.uuid4())
        model = body.get("model", self._model)

        # ---- 解析用户消息 ----
        text, media_urls = _parse_openai_messages(messages)
        if not text:
            return web.json_response(
                {"error": {"message": "No user message found", "type": "invalid_request_error", "code": 400}},
                status=400,
            )

        # ---- 检查内核就绪 ----
        if self.kernel is None or not self.kernel.is_booted:
            return web.json_response(
                {"error": {"message": "Kernel not ready", "type": "server_error", "code": 503}},
                status=503,
            )

        completion_id = _make_completion_id()

        # ============================
        # 流式模式 (stream=true)
        # ============================
        if stream:
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": _CONTENT_TYPE_SSE,
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
                },
            )
            await response.prepare(request)

            try:
                # 第一次流块：发送 role marker
                role_chunk = _build_sse_chunk(
                    "", model=model, completion_id=completion_id,
                )
                # 替换第一块的 delta 为 role 标记
                role_event = json.loads(role_chunk[len("data: "):-2])
                role_event["choices"][0]["delta"] = {"role": _ROLE_ASSISTANT}
                await response.write(f"data: {json.dumps(role_event, ensure_ascii=False)}\n\n".encode())

                # on_stream 回调：将文本增量写入 SSE
                async def _on_stream(delta: str) -> None:
                    chunk = _build_sse_chunk(delta, model=model, completion_id=completion_id)
                    await response.write(chunk.encode())

                # 调用内核处理消息
                await self.kernel.handle_message(
                    text,
                    context_id=conversation_id,
                    media=media_urls or None,
                    on_stream=_on_stream,
                    sender_id=conversation_id,
                )

                # 发送结束块（finish_reason=stop）
                final_chunk = _build_sse_chunk(
                    "", model=model, completion_id=completion_id, finish_reason="stop",
                )
                await response.write(final_chunk.encode())

                # 发送 [DONE] 标记
                await response.write(f"data: {_DONE_MARKER}\n\n".encode())

            except ConnectionResetError:
                logger.warning("HTTP 客户端断开连接 (conversation={})", conversation_id)
            except Exception:
                logger.exception("流式处理出错 (conversation={})", conversation_id)
                # 尝试发送错误 SSE 事件
                try:
                    await response.write(
                        f"data: {json.dumps({'error': 'Internal error'})}\n\n".encode()
                    )
                    await response.write(f"data: {_DONE_MARKER}\n\n".encode())
                except Exception:
                    pass

            return response

        # ============================
        # 非流式模式 (stream=false)
        # ============================
        try:
            result = await self.kernel.handle_message(
                text,
                context_id=conversation_id,
                media=media_urls or None,
                sender_id=conversation_id,
            )
            content = result.content if result else ""
        except Exception:
            logger.exception("非流式处理出错 (conversation={})", conversation_id)
            return web.json_response(
                {"error": {"message": "Internal server error", "type": "server_error", "code": 500}},
                status=500,
            )

        response_data = _build_json_response(content, model=model, completion_id=completion_id)
        return web.json_response(response_data)

    # ====== Handler: GET /v1/models ======

    async def _handle_models(self, request: web.Request) -> web.Response:
        """返回可用模型列表。"""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "auth_error", "code": 401}},
                status=401,
            )

        model_entry = {
            "id": self._model,
            "object": "model",
            "created": _make_timestamp(),
            "owned_by": "nanobee",
        }

        return web.json_response({
            "object": "list",
            "data": [model_entry],
        })

    # ====== Handler: GET /v1/models/{model} ======

    async def _handle_model_detail(self, request: web.Request) -> web.Response:
        """返回单个模型详情。"""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "auth_error", "code": 401}},
                status=401,
            )

        model_id = request.match_info.get("model", "")
        if model_id != self._model:
            return web.json_response(
                {"error": {"message": f"Model '{model_id}' not found", "type": "model_not_found", "code": 404}},
                status=404,
            )

        return web.json_response({
            "id": self._model,
            "object": "model",
            "created": _make_timestamp(),
            "owned_by": "nanobee",
            "permissions": [],
        })


__all__ = [
    "HTTPChannelPlugin",
]
