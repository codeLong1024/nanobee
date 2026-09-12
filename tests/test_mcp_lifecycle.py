"""MCP 连接生命周期行为测试（只经由 MCPManager 公开接口）。

本文件只替换 MCP transport 边界（``connect_mcp_servers``），且替身使用真实
anyio task group——因为本组用例要验证的正是 cancel scope 的宿主 task 语义，
用 AsyncMock 替换会把问题掩盖掉（被替换掉的旧 ``test_mcp_manager.py`` 即如此）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from nanobee.agent.mcp_manager import MCPManager, _ServerOwner
from nanobee.agent.tools.mcp import MCPToolWrapper
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger


def _servers(*names: str) -> dict[str, dict[str, Any]]:
    """构造若干 stdio server 配置。"""
    return {name: {"type": "stdio", "command": "echo"} for name in names}


@contextmanager
def _captured_messages() -> Iterator[list[str]]:
    """捕获 nanobee logger 的日志正文，用于断言热路径不刷屏。"""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@contextmanager
def _fake_transport_layer(
    teardown: list[str],
    *,
    fail: set[str] | None = None,
    calls: list[tuple[list[str], str | None]] | None = None,
    gate: asyncio.Event | None = None,
    exit_gate: asyncio.Event | None = None,
    teardown_raises: set[str] | None = None,
    teardown_started: list[str] | None = None,
) -> Iterator[None]:
    """把 MCP transport 边界换成「真实 anyio task group」的连接替身。

    替身刻意不启动子任务：scope 的宿主 task 归属与嵌套关系与真实 transport
    （mcp/client/stdio、sse、streamable_http）完全一致，子任务只影响关闭耗时。
    只有 scope 真正退出成功才记录 teardown——被吞掉的关闭不算「拆掉了」。

    Args:
        teardown: 连接被真正拆解时按序追加 server 名。
        fail: 模拟连接失败的 server 名集合。
        calls: 记录每次 connect 的入参（server 名列表与 default_cwd）。
        gate: 非 None 时，连接会先等待该事件（用于构造「连接中」状态）。
        exit_gate: 非 None 时，拆解连接前会等待该事件（用于构造「关闭很慢」状态）。
        teardown_raises: 拆解时抛 ``BaseExceptionGroup`` 的 server 名集合，
            模拟真实场景中 transport 子任务报错（anyio task group 退出即抛它）。
        teardown_started: 拆解**开始**时追加 server 名（早于 exit_gate 等待），
            用于断言多个 server 是否在并行拆解。
    """
    failing = fail or set()
    raising = teardown_raises or set()

    @asynccontextmanager
    async def _transport(name: str) -> Any:
        async with anyio.create_task_group():
            yield
        if teardown_started is not None:
            teardown_started.append(name)
        if exit_gate is not None:
            await exit_gate.wait()
        if name in raising:
            # 真实路径：transport 的子任务报错时，anyio task group 退出会抛
            # BaseExceptionGroup（anyio/_backends/_asyncio.py:799-801），
            # 而它不是 Exception 子类。
            raise BaseExceptionGroup(
                f"MCP transport '{name}' 子任务异常", [RuntimeError("child failed")],
            )
        teardown.append(name)

    async def _connect_mcp_servers(
        servers: dict[str, Any],
        registry: ToolRegistry,
        default_cwd: str | None = None,
    ) -> dict[str, AsyncExitStack]:
        if calls is not None:
            calls.append((sorted(servers), default_cwd))
        if gate is not None:
            await gate.wait()
        result: dict[str, AsyncExitStack] = {}
        for name in servers:
            if name in failing:
                continue
            stack = AsyncExitStack()
            await stack.__aenter__()
            await stack.enter_async_context(_transport(name))
            result[name] = stack
        return result

    with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=_connect_mcp_servers):
        yield


class TestMCPManagerState:
    """初始状态与属性。"""

    def test_no_servers(self) -> None:
        """无配置时未连接且 has_servers 为 False。"""
        mgr = MCPManager()
        assert not mgr.connected
        assert not mgr.has_servers

    def test_with_servers(self) -> None:
        """有配置但未 connect 时仍未连接。"""
        mgr = MCPManager(_servers("s1"))
        assert not mgr.connected
        assert mgr.has_servers


class TestMCPManagerConnect:
    """connect() 行为。"""

    @pytest.mark.asyncio
    async def test_without_servers_is_noop(self) -> None:
        """无服务器配置时 connect 是空操作。"""
        mgr = MCPManager()
        await mgr.connect(ToolRegistry())
        assert not mgr.connected

    @pytest.mark.asyncio
    async def test_is_idempotent(self) -> None:
        """已连接时重复 connect 不再建立新连接。"""
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, calls=calls):
            await mgr.connect(ToolRegistry())
            await mgr.connect(ToolRegistry())

        assert mgr.connected
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_passes_default_cwd_to_transport(self) -> None:
        """default_cwd 透传到连接层。"""
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, calls=calls):
            await mgr.connect(ToolRegistry(), default_cwd="/tmp")

        assert calls == [(["s1"], "/tmp")]

    @pytest.mark.asyncio
    async def test_one_failing_server_does_not_block_others(self) -> None:
        """单个 server 连接失败不影响其它 server，且不让 connect 抛出。"""
        teardown: list[str] = []
        mgr = MCPManager(_servers("good", "bad"))

        with _fake_transport_layer(teardown, fail={"bad"}):
            await mgr.connect(ToolRegistry())

        assert mgr.connected

    @pytest.mark.asyncio
    async def test_repeated_connect_is_silent_when_already_connected(self) -> None:
        """已连接状态下重复 connect 不产生连接日志、也不重新建立连接。

        kernel 每条消息都会调用 connect()，稳态下必须是零开销零噪音。
        """
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, calls=calls):
            await mgr.connect(ToolRegistry())
            with _captured_messages() as messages:
                await mgr.connect(ToolRegistry())
            await mgr.close()

        assert len(calls) == 1
        assert [message for message in messages if "MCP" in message] == []

    @pytest.mark.asyncio
    async def test_failed_connect_enters_retry_cooldown(self) -> None:
        """连接失败后进入冷却期：冷却内的消息不得重复付出完整连接尝试。

        否则挂死型故障（stdio 起不来）下每条消息都重新走一次最长
        ``CONNECT_ATTEMPT_TIMEOUT_S`` 的建连，把整轮对话拖住。
        """
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"), retry_cooldown_s=60.0)

        with _fake_transport_layer(teardown, calls=calls, fail={"s1"}):
            await mgr.connect(ToolRegistry())
            await mgr.connect(ToolRegistry())  # 冷却期内：跳过，不再尝试

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cooldown_elapsed_allows_retry(self) -> None:
        """冷却期满后重试照常进行（retry_cooldown_s=0 等价于冷却立即结束）。"""
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"), retry_cooldown_s=0.0)

        with _fake_transport_layer(teardown, calls=calls, fail={"s1"}):
            await mgr.connect(ToolRegistry())
            await mgr.connect(ToolRegistry())  # 冷却为 0：立即重试

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_concurrent_connect_waits_for_same_connection(self) -> None:
        """连接仍在进行时，第二个调用者必须等待而不是直接返回。

        否则首轮消息会在 MCP 工具尚未注册时发出（首轮工具不可见的竞态）。
        """
        teardown: list[str] = []
        gate = asyncio.Event()
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, gate=gate):
            first = asyncio.create_task(mgr.connect(ToolRegistry()))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(mgr.connect(ToolRegistry()))
            await asyncio.sleep(0.01)

            assert not second.done()

            gate.set()
            await asyncio.gather(first, second)

        assert mgr.connected


class TestMCPManagerClose:
    """close() 行为。"""

    @pytest.mark.asyncio
    async def test_tears_down_every_connected_server(self) -> None:
        """close() 必须真正拆掉每一个已连接的 server。

        线上症状：``MCP 服务器 '...' 清理错误（可忽略）`` —— 关闭被静默吞掉，
        先进入的那个 server 永远没被拆解（连接与子进程泄漏）。
        """
        teardown: list[str] = []
        mgr = MCPManager(_servers("s1", "s2"))

        with _fake_transport_layer(teardown):
            await mgr.connect(ToolRegistry())
            await mgr.close()

        assert sorted(teardown) == ["s1", "s2"]
        assert not mgr.connected

    @pytest.mark.asyncio
    async def test_is_idempotent(self) -> None:
        """重复 close 不报错。"""
        teardown: list[str] = []
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown):
            await mgr.connect(ToolRegistry())
            await mgr.close()
            await mgr.close()

        assert teardown == ["s1"]

    @pytest.mark.asyncio
    async def test_reconnect_after_close_works(self) -> None:
        """close 之后可以重新 connect（open→close→open 全流程）。"""
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, calls=calls):
            await mgr.connect(ToolRegistry())
            await mgr.close()
            await mgr.connect(ToolRegistry())
            await mgr.close()

        assert teardown == ["s1", "s1"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_connect_during_close_does_not_leak_connection(self) -> None:
        """关闭进行中收到的 connect 不得建立第二条连接。

        否则 shutdown 清空登记后，新连接会逃过回收（连接与子进程泄漏，
        退出时还会留下 pending task）。
        """
        teardown: list[str] = []
        calls: list[tuple[list[str], str | None]] = []
        exit_gate = asyncio.Event()
        mgr = MCPManager(_servers("s1"))

        with _fake_transport_layer(teardown, calls=calls, exit_gate=exit_gate):
            await mgr.connect(ToolRegistry())

            closing = asyncio.create_task(mgr.close())
            await asyncio.sleep(0.01)  # 让 close 进入拆解阶段（卡在 exit_gate）

            await mgr.connect(ToolRegistry())  # 在途消息触发

            exit_gate.set()
            await closing

        assert len(calls) == 1  # 没有建立第二条连接
        assert not mgr.connected
        assert teardown == ["s1"]

    @pytest.mark.asyncio
    async def test_owner_revived_after_close_can_be_closed_again(self) -> None:
        """close 超时 → owner 自行拆完退出 → connect 复活 → 再 close 必须生效。

        回归锁：``start()`` 重建 owner task 时必须复位 ``_closing``。否则复活的
        owner 会被「上一世的关闭请求」永久豁免——后续所有 close 都在守卫处直接
        返回，连接与子进程泄漏到进程退出（且 owner 仍存活，登记永不摘除）。
        """
        teardown: list[str] = []
        calls: list[list[str]] = []
        exit_gate = asyncio.Event()
        mgr = MCPManager(_servers("s1"))

        with (
            patch("nanobee.agent.mcp_manager.CLOSE_WAIT_TIMEOUT_S", 0.05),
            _fake_transport_layer(teardown, calls=calls, exit_gate=exit_gate),
        ):
            await mgr.connect(ToolRegistry())  # 第 1 条连接
            await mgr.close()  # 外部等待超时（拆解卡在 exit_gate），登记保留

            exit_gate.set()  # 放行：owner 拆完后退出
            task = mgr._owners["s1"]._task
            assert task is not None
            await asyncio.wait_for(task, timeout=1)  # 等 owner 真正退出

            await mgr.connect(ToolRegistry())  # 复活：重建 owner、建第 2 条连接
            assert len(calls) == 2

            await mgr.close()  # 二次 close：必须真正拆解（_closing 已复位）

        assert teardown == ["s1", "s1"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_close_timeout_keeps_owner_registered(self) -> None:
        """关闭超时后不得摘除 owner 登记。

        超时的 owner 仍存活且持有活跃连接，摘除后它只被自己的 pending task 引用：
        连接、子进程、task 三重泄漏，且下一次 connect() 会另建第二条连接——
        正是「宁可停不稳也不重叠」要避免的情况。
        """
        teardown: list[str] = []
        calls: list[list[str]] = []
        exit_gate = asyncio.Event()  # 永不置位 → 拆解必然超时
        mgr = MCPManager(_servers("s1"))

        with (
            patch("nanobee.agent.mcp_manager.CLOSE_WAIT_TIMEOUT_S", 0.05),
            _fake_transport_layer(teardown, calls=calls, exit_gate=exit_gate),
        ):
            await mgr.connect(ToolRegistry())
            await mgr.close()  # 超时：放弃等待但不摘除登记

            await mgr.connect(ToolRegistry())  # 不得另建连接

            exit_gate.set()  # 放行，避免留下悬挂 task
            await asyncio.sleep(0.05)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_transport_teardown_exception_group_is_contained(self) -> None:
        """transport 拆解抛组异常时必须就地收住并如实记日志。

        ⚠️ 组异常的形态取决于**成员**（CPython 语义）：
        ``BaseExceptionGroup(msg, [RuntimeError(...)])`` 的构造结果是
        ``ExceptionGroup``——成员全是 ``Exception`` 时自动降级，而
        ``ExceptionGroup`` **是** ``Exception`` 子类。只有含非 ``Exception``
        成员（如 ``CancelledError``）时才是真正的 ``BaseExceptionGroup``。
        anyio 的 ``_spawn`` / ``TaskGroup.__aexit__`` 都会把 ``CancelledError``
        过滤掉，故 transport 子任务报错走的是前一种形态，本就被
        ``except Exception`` 兜住。

        保留本用例是为了锁住行为契约：拆解失败必须记「连接可能未被完整回收」，
        而不是逃到 ``_serve`` 的兜底大网被误报为「owner task 异常退出」。
        """
        teardown: list[str] = []
        mgr = MCPManager(_servers("s1", "s2"))

        with _fake_transport_layer(teardown, teardown_raises={"s1"}):
            with _captured_messages() as messages:
                await mgr.connect(ToolRegistry())
                await mgr.close()

        assert any("关闭时出错" in m for m in messages), "应就地收住并记「连接可能未被完整回收」"
        assert not any("owner task 异常退出" in m for m in messages), "不应逃到 owner 的兜底大网"
        assert teardown == ["s2"], "单个 server 拆解失败不得影响其它 server"

    @pytest.mark.asyncio
    async def test_close_does_not_raise_on_owner_group_exception(self) -> None:
        """单个 owner 关闭抛组异常时，close() 必须收住并继续。

        否则异常会穿透 ``MCPManager.close()`` 一路上抛到 ``kernel.shutdown()``
        （该段没有 try/except），中断其后的会话落盘与插件卸载。
        """
        mgr = MCPManager(_servers("s1"))
        owner = _ServerOwner("s1", mgr._servers["s1"], ToolRegistry())

        async def _boom() -> None:
            raise BaseExceptionGroup("owner 关闭失败", [RuntimeError("boom")])

        owner.request_close = _boom  # type: ignore[method-assign]
        mgr._owners["s1"] = owner

        with _captured_messages() as messages:
            await mgr.close()

        assert any("关闭异常" in m for m in messages)

    @pytest.mark.asyncio
    async def test_close_is_parallel_across_owners(self) -> None:
        """各 owner 相互独立（这正是每 server 一个 task 的核心收益），关闭应并行。

        串行实现下总耗时随 server 数线性放大（N × CLOSE_WAIT_TIMEOUT_S），
        而 systemd 的关停预算是宝贵资源。
        """
        teardown: list[str] = []
        started: list[str] = []
        exit_gate = asyncio.Event()
        mgr = MCPManager(_servers("s1", "s2"))

        with _fake_transport_layer(
            teardown, exit_gate=exit_gate, teardown_started=started,
        ):
            await mgr.connect(ToolRegistry())

            closing = asyncio.create_task(mgr.close())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.5
            while len(started) < 2 and loop.time() < deadline:
                await asyncio.sleep(0.01)

            try:
                assert sorted(started) == ["s1", "s2"], "两个 owner 必须同时在拆解（并行）"
            finally:
                exit_gate.set()
                await closing

        assert sorted(teardown) == ["s1", "s2"]


class TestMCPReconnect:
    """会话终止后的重连行为。"""

    @pytest.mark.asyncio
    async def test_session_termination_rebuilds_connection(self) -> None:
        """工具调用命中「session terminated」时自动重连并重试成功。

        重连必须由「进入该连接的同一个 task」执行，否则 anyio 会抛
        ``Attempted to exit cancel scope in a different task ...``。
        """
        teardown: list[str] = []
        attempts = {"n": 0}

        @asynccontextmanager
        async def _transport(name: str) -> Any:
            async with anyio.create_task_group():
                yield
            teardown.append(name)

        class _Session:
            """第一次连接返回「已终止」的会话，之后返回可用会话。"""

            def __init__(self, attempt: int) -> None:
                self._attempt = attempt

            async def call_tool(self, name: str, arguments: Any = None) -> Any:
                if self._attempt == 1:
                    raise RuntimeError("session terminated")
                return SimpleNamespace(content=["pong"])

        async def _connect_mcp_servers(
            servers: dict[str, Any],
            registry: ToolRegistry,
            default_cwd: str | None = None,
        ) -> dict[str, AsyncExitStack]:
            result: dict[str, AsyncExitStack] = {}
            for name in servers:
                attempts["n"] += 1
                stack = AsyncExitStack()
                await stack.__aenter__()
                await stack.enter_async_context(_transport(name))
                tool_def = SimpleNamespace(
                    name="ping", description="ping", inputSchema={"type": "object", "properties": {}},
                )
                registry.register(
                    MCPToolWrapper(_Session(attempts["n"]), name, tool_def),
                )
                result[name] = stack
            return result

        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=_connect_mcp_servers):
            await mgr.connect(registry)
            tool = registry.get("mcp_s1_ping")
            assert tool is not None

            result = await tool.execute()

            assert "pong" in result
            assert teardown == ["s1"]  # 旧连接在同 task 内被拆解
            assert attempts["n"] == 2  # 确实重建过一次连接

            await mgr.close()

        assert sorted(teardown) == ["s1", "s1"]


class _TerminatedSession:
    """总是报「会话终止」的假 session，用于触发重连路径。"""

    async def call_tool(self, name: str, arguments: Any = None) -> Any:
        raise RuntimeError("session terminated")


class _HealthySession:
    """可用 session：重连成功后的调用应当走通。"""

    async def call_tool(self, name: str, arguments: Any = None) -> Any:
        return SimpleNamespace(content=["pong"])


@contextmanager
def _terminated_server_layer(
    teardown: list[str],
    *,
    tool_names: tuple[str, ...] = ("ping",),
    calls: list[list[str]] | None = None,
    fail_calls: set[int] | None = None,
    hang_calls: set[int] | None = None,
    hang_gate: asyncio.Event | None = None,
    delay_calls: dict[int, float] | None = None,
    healthy_after: int | None = None,
) -> Iterator[None]:
    """连接替身：建立 s1 并注册若干「会话已终止」的 MCP 工具。

    Args:
        teardown: 连接被真正拆解时按序追加 server 名。
        tool_names: 每次连接为 s1 注册的工具名（对应 ``mcp_s1_<name>``）。
        calls: 记录每次连接的 server 名列表。
        fail_calls: 第 N 次连接返回空（模拟连接失败）。
        hang_calls: 第 N 次连接挂起（模拟 stdio 启动卡死），由 ``hang_gate`` 放行。
        hang_gate: 放行 ``hang_calls`` 的事件。
        delay_calls: 第 N 次连接先延迟若干秒再建立（模拟子进程冷启动慢但能成功）。
        healthy_after: 从第 N 次连接起注册可用 session（模拟重连后真的恢复了）。
    """
    attempt = {"n": 0}

    @asynccontextmanager
    async def _transport(name: str) -> Any:
        async with anyio.create_task_group():
            yield
        teardown.append(name)

    async def _connect_mcp_servers(
        servers: dict[str, Any],
        registry: ToolRegistry,
        default_cwd: str | None = None,
    ) -> dict[str, AsyncExitStack]:
        attempt["n"] += 1
        if calls is not None:
            calls.append(sorted(servers))
        if delay_calls and attempt["n"] in delay_calls:
            await asyncio.sleep(delay_calls[attempt["n"]])
        if fail_calls and attempt["n"] in fail_calls:
            return {}
        if hang_calls and attempt["n"] in hang_calls:
            assert hang_gate is not None
            await hang_gate.wait()
            return {}
        healthy = healthy_after is not None and attempt["n"] >= healthy_after
        result: dict[str, AsyncExitStack] = {}
        for name in servers:
            stack = AsyncExitStack()
            await stack.__aenter__()
            await stack.enter_async_context(_transport(name))
            for tool_name in tool_names:
                tool_def = SimpleNamespace(
                    name=tool_name,
                    description=tool_name,
                    inputSchema={"type": "object", "properties": {}},
                )
                session: Any = _HealthySession() if healthy else _TerminatedSession()
                registry.register(MCPToolWrapper(session, name, tool_def))
            result[name] = stack
        return result

    with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=_connect_mcp_servers):
        yield


@contextmanager
def _hanging_stdio_layer(teardown: list[str], gate: asyncio.Event) -> Iterator[None]:
    """真实 ``connect_mcp_servers`` + 替身 transport/ClientSession：卡在 initialize。

    与其它替身不同，这里**不替换** ``connect_mcp_servers``——本用例要验证的正是
    ``connect_single_server`` 在取消（超时）路径上是否回收半成品 AsyncExitStack。

    Args:
        teardown: 半成品 stack 被回收时按序追加 ``session`` / ``transport``。
        gate: 卡住 ``initialize()`` 的事件（放行后连接才会继续）。
    """

    @asynccontextmanager
    async def _fake_stdio_client(params: Any) -> Any:
        async with anyio.create_task_group():
            yield object(), object()
        teardown.append("transport")

    class _FakeSession:
        def __init__(self, read: Any, write: Any) -> None:
            self._tg: Any = None

        async def __aenter__(self) -> Any:
            self._tg = anyio.create_task_group()
            await self._tg.__aenter__()
            return self

        async def __aexit__(self, *exc_info: Any) -> None:
            await self._tg.__aexit__(*exc_info)
            teardown.append("session")

        async def initialize(self) -> None:
            await gate.wait()

    with (
        patch("mcp.client.stdio.stdio_client", new=_fake_stdio_client),
        patch("mcp.ClientSession", new=_FakeSession),
    ):
        yield


class TestMCPOwnerNeverHangsCaller:
    """owner task 无论如何都不得让调用方永久等待。"""

    @pytest.mark.asyncio
    async def test_non_exception_error_during_connect_does_not_hang(self) -> None:
        """连接阶段抛 BaseExceptionGroup 时 connect 必须返回。

        anyio 的 task group 抛的 ``BaseExceptionGroup`` 不是 ``Exception`` 子类，
        只有 ``except BaseException`` 能兜住；漏兜会让 owner task 静默死亡、
        ``ready`` 永不置位，进而把 ``connect()``（即启动路径）永久挂住。
        """

        async def _connect_mcp_servers(
            servers: dict[str, Any],
            registry: ToolRegistry,
            default_cwd: str | None = None,
        ) -> dict[str, AsyncExitStack]:
            raise BaseExceptionGroup("boom", [asyncio.CancelledError()])

        mgr = MCPManager(_servers("s1"))

        with patch("nanobee.agent.tools.mcp.connect_mcp_servers", new=_connect_mcp_servers):
            await asyncio.wait_for(mgr.connect(ToolRegistry()), timeout=2)

        assert not mgr.connected

    @pytest.mark.asyncio
    async def test_error_during_reconnect_does_not_hang_tool_call(self) -> None:
        """重连路径抛异常时，工具调用必须拿到结果而不是永久挂起。"""
        teardown: list[str] = []
        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        with _terminated_server_layer(teardown):
            await mgr.connect(registry)
            tool = registry.get("mcp_s1_ping")
            assert tool is not None

            with patch(
                "nanobee.agent.tools.mcp.unregister_server_tools",
                side_effect=RuntimeError("boom"),
            ):
                result = await asyncio.wait_for(tool.execute(), timeout=2)

            assert isinstance(result, str)
            await mgr.close()

        assert teardown == ["s1"]

    @pytest.mark.asyncio
    async def test_stale_wrapper_does_not_rebuild_again(self) -> None:
        """同一 server 上另一个（已过期的）wrapper 再触发时不得重复拆建连接。

        否则并发的会话终止会把刚建好的连接再拆一次，打断正在进行中的调用。
        """
        teardown: list[str] = []
        calls: list[list[str]] = []
        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        with _terminated_server_layer(teardown, tool_names=("ping", "pong"), calls=calls):
            await mgr.connect(registry)
            stale_a = registry.get("mcp_s1_ping")
            stale_b = registry.get("mcp_s1_pong")
            assert stale_a is not None
            assert stale_b is not None

            await stale_a.execute()  # 触发第一次重连
            assert len(calls) == 2

            await stale_b.execute()  # 过期 wrapper 再触发：不应再拆建
            assert len(calls) == 2

            await mgr.close()


class TestMCPRecovery:
    """连接丢失后的恢复语义。"""

    @pytest.mark.asyncio
    async def test_failed_reconnect_is_retried_on_next_connect(self) -> None:
        """重连失败后，下一次 connect() 必须真正重试并恢复可用。

        否则 start() 返回首次连接时置位的过期成功结果：该 server 的工具已被
        注销、再无触发者，于是直到进程重启都不可用，还假报「成功连接」。
        """
        teardown: list[str] = []
        calls: list[list[str]] = []
        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        # 第 2 次连接（即重连）失败，第 3 次恢复
        with _terminated_server_layer(teardown, calls=calls, fail_calls={2}):
            await mgr.connect(registry)
            tool = registry.get("mcp_s1_ping")
            assert tool is not None

            await tool.execute()  # 触发重连 → 失败
            assert len(calls) == 2
            assert not mgr.connected

            await mgr.connect(registry)  # 下一条消息：必须真正重试
            assert len(calls) == 3
            assert mgr.connected
            assert registry.get("mcp_s1_ping") is not None

            await mgr.close()

    @pytest.mark.asyncio
    async def test_reconnect_wait_covers_connect_budget(self) -> None:
        """重连的等待预算必须覆盖「连接预算」，否则慢启动 server 的重连必然假超时。

        重连 = ``_do_close()`` + ``_open()``，而 ``_open()`` 的机制上界是
        ``CONNECT_ATTEMPT_TIMEOUT_S``（含子进程冷启动 + initialize 握手）。
        若调用方只等 ``PER_SERVER_OP_TIMEOUT_S``（拆解量级），npx 类 server 的
        重连几乎必然假超时：调用方拿到 None → 工具回报 could not refresh
        session，尽管 owner 稍后其实重连成功了。

        本用例把两个预算分别压小以放大差异：拆解预算 0.05s、重连预算 1.0s，
        实际重连耗时 0.2s —— 修复前必然假超时。
        """
        teardown: list[str] = []
        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        with (
            patch("nanobee.agent.mcp_manager.PER_SERVER_OP_TIMEOUT_S", 0.05),
            patch("nanobee.agent.mcp_manager.RECONNECT_WAIT_TIMEOUT_S", 1.0),
            _terminated_server_layer(teardown, delay_calls={2: 0.2}, healthy_after=2),
        ):
            await mgr.connect(registry)
            tool = registry.get("mcp_s1_ping")
            assert tool is not None

            with _captured_messages() as messages:
                result = await tool.execute()

            assert "pong" in result, "慢启动重连成功后调用必须成功，不得假报超时"
            assert not any("重连超时" in m for m in messages)

            await mgr.close()

    @pytest.mark.asyncio
    async def test_hanging_reconnect_returns_within_timeout(self) -> None:
        """重连挂起时工具调用必须超时返回，不能拖死整个 turn。

        重连是在 ``_execute_with_retry`` 的 except 分支里 await 的——那里没有
        其它超时保护，stdio 启动卡死会一路挂到 agent turn。
        """
        teardown: list[str] = []
        hang_gate = asyncio.Event()
        mgr = MCPManager(_servers("s1"))
        registry = ToolRegistry()

        with (
            patch("nanobee.agent.mcp_manager.RECONNECT_WAIT_TIMEOUT_S", 0.05),
            _terminated_server_layer(teardown, hang_calls={2}, hang_gate=hang_gate),
        ):
            await mgr.connect(registry)
            tool = registry.get("mcp_s1_ping")
            assert tool is not None

            # 重连（第 2 次连接）永久挂起：必须在超时内返回，而不是挂死
            result = await asyncio.wait_for(tool.execute(), timeout=2)

            assert isinstance(result, str)

            hang_gate.set()  # 放行，避免留下悬挂 task
            await asyncio.sleep(0.05)
            await mgr.close()

    @pytest.mark.asyncio
    async def test_hanging_connect_is_bounded_and_cleans_up_half_built_stack(self) -> None:
        """连接尝试必须有上界，且超时取消时要回收半成品 stack。

        卡死的 stdio server 不能让 connect()（每条消息都会调用）无限等待；
        取消路径若不关闭 AsyncExitStack，会留下活跃 scope 与未回收的子进程/子任务。
        """
        teardown: list[str] = []
        gate = asyncio.Event()
        mgr = MCPManager({"s1": {"type": "stdio", "command": "echo"}})

        with (
            patch("nanobee.agent.mcp_manager.CONNECT_ATTEMPT_TIMEOUT_S", 0.05),
            _hanging_stdio_layer(teardown, gate),
        ):
            await asyncio.wait_for(mgr.connect(ToolRegistry()), timeout=2)

            assert not mgr.connected
            assert teardown == ["session", "transport"]  # 半成品 stack 已回收

        gate.set()
        await asyncio.sleep(0.05)
        await mgr.close()
