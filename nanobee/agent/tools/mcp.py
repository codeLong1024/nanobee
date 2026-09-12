"""MCP client: connects to MCP servers and wraps their tools as native nanobee tools."""

import asyncio
import os
import re
import shutil
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from typing import Any

import httpx
from nanobee.utils.logger import logger


from nanobee.agent.tools.base import Tool
from nanobee.agent.tools.registry import ToolRegistry
from pydantic import BaseModel


class MCPServerConfig(BaseModel):
    """MCP 服务器配置模型。"""

    type: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled_tools: list[str] | None = None
    tool_timeout: int = 30
    headers: dict[str, str] | None = None


def _dict_to_mcp_config(cfg_dict: dict[str, Any]) -> MCPServerConfig:
    """将字典转换为 MCP 服务器配置模型。"""
    return MCPServerConfig(**cfg_dict)

# 标准库中可以用 isinstance 匹配的瞬态连接异常
_TRANSIENT_EXC_TYPES: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionRefusedError,
    ConnectionAbortedError,
    ConnectionError,
)

# 第三方库（如 anyio）中的异常类名，只能用字符串匹配
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _redact_url(url: str | None) -> str:
    """日志用 URL 脱敏：剥离 query 与 fragment（MCP 网关的 URL query 常携带 key）。"""
    if not url:
        return str(url)
    return url.split("?", 1)[0].split("#", 1)[0]

_ReconnectCallback = Callable[[str, str, Tool], Awaitable[Tool | None]]


def _is_session_terminated(exc: BaseException) -> bool:
    """检测 MCP SDK 报告的会话终止错误。"""
    messages = [str(exc)]
    error = getattr(exc, "error", None)
    if error is not None:
        messages.append(str(getattr(error, "message", "")))
    return any(
        marker in message.lower()
        for marker in ("session terminated", "connection closed")
        for message in messages
    )


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error.

    Uses isinstance checks against stdlib types for correctness,
    and falls back to class-name matching for exceptions from
    third-party libraries (e.g. anyio ClosedResourceError).
    """
    if isinstance(exc, _TRANSIENT_EXC_TYPES):
        return True
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """Quick TCP probe to check if an HTTP MCP server is reachable.

    Avoids entering ``streamable_http_client`` / ``sse_client`` when the port is
    closed — those transports use anyio task groups whose cleanup can raise
    ``RuntimeError`` / ``ExceptionGroup`` that escape the caller's try/except
    and crash the event loop.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        # 关闭可能抛出或挂起（尤其是对端已异常断连的 socket），
        # 用 suppress 包裹并加短超时，避免探测本身破坏事件循环。
        with suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            # 同时移除 oneOf 和 anyOf，避免处理 oneOf 后 anyOf 残留
            merged = {k: v for k, v in normalized.items() if k not in ("oneOf", "anyOf")}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class _MCPWrapperBase(Tool):
    """公共基类，为绑定到同一 MCP 服务器会话的 Wrapper 提供重连支持。"""

    def _set_mcp_connection(self, session: Any, server_name: str) -> None:
        self._session = session
        self._server_name = server_name
        self._reconnect: _ReconnectCallback | None = None

    def set_reconnect_handler(self, reconnect: _ReconnectCallback) -> None:
        self._reconnect = reconnect

    @property
    def server_name(self) -> str:
        """该 wrapper 绑定的 MCP 服务器名（注销与重连回调按它精确归属）。"""
        return self._server_name

    async def _refresh_session_after_termination(
        self,
        exc: BaseException,
        already_refreshed: bool,
        capability_kind: str,
    ) -> bool:
        if already_refreshed or not _is_session_terminated(exc) or self._reconnect is None:
            return False
        logger.warning(
            "MCP {} '{}' session terminated; reconnecting server '{}' before retry",
            capability_kind,
            self._name,
            self._server_name,
        )
        refreshed_tool = await self._reconnect(self._server_name, self._name, self)
        refreshed_session = getattr(refreshed_tool, "_session", None)
        if refreshed_session is None:
            logger.warning(
                "MCP {} '{}' could not refresh session for server '{}'",
                capability_kind,
                self._name,
                self._server_name,
            )
            return False
        self._session = refreshed_session
        return True

    async def _execute_with_retry(
        self,
        call_fn: Callable[[], Awaitable[Any]],
        extract_fn: Callable[[Any], str],
        capability_kind: str,
        timeout: int,
        specific_error_handler: Callable[[BaseException], str | None] | None = None,
    ) -> str:
        """共享的重试循环：超时 → 取消 → 会话刷新 → 瞬态错误重试。

        Args:
            call_fn: 实际的 MCP 调用协程。
            extract_fn: 从成功结果中提取字符串。
            capability_kind: 能力名称（tool / resource / prompt），用于日志。
            timeout: 单次调用的超时秒数。
            specific_error_handler: 可选，在标准 retry/refresh 逻辑之前调用的
                异常处理器。返回 str 表示已处理（作为最终结果），返回 None
                则继续走通用逻辑。
        """
        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(call_fn(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP {} '{}' timed out after {}s", capability_kind, self._name, timeout,
                )
                return f"(MCP {capability_kind} call timed out after {timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning(
                    "MCP {} '{}' was cancelled by server/SDK", capability_kind, self._name,
                )
                return f"(MCP {capability_kind} call was cancelled)"
            except Exception as exc:
                # 调用方特定的异常处理（如 McpError 中的详细错误码）
                if specific_error_handler is not None:
                    handled = specific_error_handler(exc)
                    if handled is not None:
                        return handled
                if await self._refresh_session_after_termination(
                    exc, refreshed_session, capability_kind,
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP {} '{}' hit transient error ({}), retrying once...",
                            capability_kind, self._name, type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP {} '{}' failed after retry: {}",
                        capability_kind, self._name, type(exc).__name__,
                    )
                    return f"(MCP {capability_kind} call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP {} '{}' failed: {}: {}",
                    capability_kind, self._name, type(exc).__name__, exc,
                )
                return f"(MCP {capability_kind} call failed: {type(exc).__name__})"
            else:
                return extract_fn(result)

        # 不可达：循环体内仅通过 return 退出，此处作为防御性兜底显式抛出
        raise RuntimeError(
            f"MCP {capability_kind} '{self._name}' retry loop exited unexpectedly"
        )


class MCPToolWrapper(_MCPWrapperBase):
    """Wraps a single MCP server tool as a nanobee Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        async def _call() -> Any:
            return await self._session.call_tool(self._original_name, arguments=kwargs)

        def _extract(result: Any) -> str:
            parts = []
            for block in result.content:
                if isinstance(block, types.TextContent):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            return "\n".join(parts) or "(no output)"

        return await self._execute_with_retry(_call, _extract, "tool", self._tool_timeout)


class MCPResourceWrapper(_MCPWrapperBase):
    """Wraps an MCP resource URI as a read-only nanobee Tool."""

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        async def _call() -> Any:
            return await self._session.read_resource(self._uri)

        def _extract(result: Any) -> str:
            parts: list[str] = []
            for block in result.contents:
                if isinstance(block, types.TextResourceContents):
                    parts.append(block.text)
                elif isinstance(block, types.BlobResourceContents):
                    parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) or "(no output)"

        return await self._execute_with_retry(_call, _extract, "resource", self._resource_timeout)


class MCPPromptWrapper(_MCPWrapperBase):
    """Wraps an MCP prompt as a read-only nanobee Tool."""

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        from mcp.shared.exceptions import McpError

        async def _call() -> Any:
            return await self._session.get_prompt(self._prompt_name, arguments=kwargs)

        def _extract(result: Any) -> str:
            parts: list[str] = []
            for message in result.messages:
                content = message.content
                if isinstance(content, types.TextContent):
                    parts.append(content.text)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, types.TextContent):
                            parts.append(block.text)
                        else:
                            parts.append(str(block))
                else:
                    parts.append(str(content))
            return "\n".join(parts) or "(no output)"

        def _handle_mcp_error(exc: BaseException) -> str | None:
            """返回 McpError 的带错误码的详细消息。"""
            if not isinstance(exc, McpError):
                return None
            logger.exception(
                "MCP prompt '{}' failed: code={} message={}",
                self._name,
                exc.error.code,
                exc.error.message,
            )
            return f"(MCP prompt call failed: {exc.error.message} [code {exc.error.code}])"

        return await self._execute_with_retry(
            _call, _extract, "prompt", self._prompt_timeout,
            specific_error_handler=_handle_mcp_error,
        )


async def connect_mcp_servers(
    mcp_servers: dict[str, dict[str, Any] | MCPServerConfig],
    registry: ToolRegistry,
    default_cwd: str | None = None,
) -> dict[str, AsyncExitStack]:
    """Connect to configured MCP servers and register their tools, resources, prompts.

    Returns a dict mapping server name -> its dedicated AsyncExitStack.

    Note:
        一次只允许传入一个 server，且必须从「该 server 的 owner task」中调用
        （入口处有运行时断言）：anyio 的 cancel scope 归属进入它的那个 task，
        且同一 task 内并发持有的多个 scope 严格嵌套、只能整体逆序退出——无法
        只关中间某一个，重连语义不成立。多 server 的编排由 ``MCPManager``
        （每 server 一个 owner task）负责。
    """
    if len(mcp_servers) > 1:
        # 结构性约束的运行时断言：docstring 约定无法阻止未来的误用，误用会
        # 直接复现「一个 task 管多个 server」的 cancel scope 结构性错误。
        raise ValueError(
            "connect_mcp_servers 一次只允许连接一个 server（多 server 编排归 MCPManager）"
        )
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            if transport_type == "stdio":
                command, args, env = _normalize_windows_stdio_command(
                    cfg.command,
                    cfg.args,
                    cfg.env or None,
                )
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                    cwd=cfg.cwd or default_cwd,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                if not await _probe_http_url(cfg.url):
                    logger.warning(
                        "MCP server '{}': {} unreachable, skipping", name, _redact_url(cfg.url)
                    )
                    await server_stack.aclose()
                    return name, None

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                if not await _probe_http_url(cfg.url):
                    logger.warning(
                        "MCP server '{}': {} unreachable, skipping", name, _redact_url(cfg.url)
                    )
                    await server_stack.aclose()
                    return name, None

                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools or ["*"])
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if tool_def.name in enabled_tools:
                    matched_enabled_tools.add(tool_def.name)
                if wrapped_name in enabled_tools:
                    matched_enabled_tools.add(wrapped_name)

                if enabled_tools and not allow_all_tools:
                    unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                    if unmatched_enabled_tools:
                        # 仅在确有未匹配项时才构建可用名列表，避免常态下的无用开销
                        available_raw_names = [tool_def.name for tool_def in tools.tools]
                        available_wrapped_names = [
                            _sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools
                        ]
                        logger.warning(
                            "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                            "Available wrapped names: {}",
                            name,
                            ", ".join(unmatched_enabled_tools),
                            ", ".join(available_raw_names) or "(none)",
                            ", ".join(available_wrapped_names) or "(none)",
                        )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug(
                    "MCP server '{}': resources not supported or failed: {}",
                    name,
                    type(e).__name__,
                )

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug(
                    "MCP server '{}': prompts not supported or failed: {}",
                    name,
                    type(e).__name__,
                )

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            return name, server_stack

        except asyncio.CancelledError:
            # 取消（连接超时/关闭）路径同样要回收半成品 stack：transport 与
            # ClientSession 的 task group 已经 enter，不关闭会留下活跃 scope
            # 以及未回收的子进程、子任务。
            with suppress(asyncio.CancelledError, Exception):
                await server_stack.aclose()
            raise
        except Exception as e:
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            # 脱敏责任归本层：第三方（httpx/anyio）的异常消息内嵌完整请求 URL
            # （query 携带网关 key），而 logger.exception 会把 traceback 连同
            # str(exc) 一起写进日志。往下的第三方文案改不了，这里是第一个
            # nanobee 自有边界，必须在此收口——故不用 exception，只记异常类名
            # 与脱敏后的 URL。
            logger.error(
                "MCP server '{}': failed to connect: {} url={}{}",
                name,
                type(e).__name__,
                _redact_url(cfg.url),
                hint,
            )
            with suppress(Exception):
                await server_stack.aclose()
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    for name, cfg in mcp_servers.items():
        # 将字典转换为 Pydantic 模型
        if isinstance(cfg, dict):
            cfg = _dict_to_mcp_config(cfg)
        try:
            result = await connect_single_server(name, cfg)
        except Exception as e:
            # 同上：不输出原始异常，避免 traceback 带出含 key 的完整 URL。
            logger.error(
                "MCP server '{}' connection failed: {} url={}",
                name,
                type(e).__name__,
                _redact_url(cfg.url),
            )
            continue
        if result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks


def unregister_server_tools(registry: ToolRegistry, server_name: str) -> int:
    """从注册表中注销指定 MCP 服务器注册的全部工具。

    按 wrapper 的归属（``server_name`` 精确匹配）而不是名称前缀：前缀匹配在
    server 名互为前缀（如 ``a`` 与 ``a_b``）时会误删对方的工具。

    Args:
        registry: 工具注册表。
        server_name: 服务器名。

    Returns:
        注销的工具数量。
    """
    removed = 0
    for tool_name in list(registry.tool_names):
        tool = registry.get(tool_name)
        if isinstance(tool, _MCPWrapperBase) and tool.server_name == server_name:
            registry.unregister(tool_name)
            removed += 1
    return removed


def attach_reconnect_handlers(
    registry: ToolRegistry,
    server_names: list[str],
    reconnect: _ReconnectCallback,
) -> None:
    """为指定服务器的所有 MCP Wrapper 注入重连回调。

    回调由调用方提供：MCP 连接的重连必须在「进入该连接的同一个 task」里执行
    （anyio 的 cancel scope 归属宿主 task），因此本函数只负责把回调挂到工具
    上，不决定由谁重连。匹配同样按 wrapper 归属而非名称前缀（理由同
    unregister_server_tools）。

    Args:
        registry: 工具注册表。
        server_names: 要挂接回调的服务器名列表。
        reconnect: 重连回调（由对应 server 的 owner task 提供）。
    """
    wanted = set(server_names)
    for tool_name in list(registry.tool_names):
        tool = registry.get(tool_name)
        if isinstance(tool, _MCPWrapperBase) and tool.server_name in wanted:
            tool.set_reconnect_handler(reconnect)
