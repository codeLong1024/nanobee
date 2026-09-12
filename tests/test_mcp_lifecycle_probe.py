"""MCP 生命周期设计的 anyio 约束探针。

本文件不引入 mcp SDK，只用真实 anyio task group 复现「一个 task 内并发持有多个
cancel scope」的硬约束，作为 MCP 生命周期重构（每 server 一个 owner task）的
设计前提证据。

为什么需要这组用例：anyio 的 ``CancelScope`` 归属「进入它的那个 task」，且同一
task 内的多个 scope 严格嵌套。MCP 的每个连接（stdio / sse / streamableHttp）
都会进入一个 task group，因此「一个 task 连 N 个 server」这一结构在关闭语义上
根本不成立。以下 4 条用例把该结论固化，防止将来有人把 owner 结构简化回
「单 owner 管所有 server」。

这些用例断言的是 anyio 自身行为，不随 nanobee 内部重构变化；若哪天它们失败，
说明 anyio 语义变更，MCP 生命周期设计需要重新评审。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import pytest


@asynccontextmanager
async def _fake_transport() -> Any:
    """模拟一个 MCP transport：在 anyio task group 内 yield。

    真实实现同样是「进入 task group 后 yield」——参见
    ``mcp/client/stdio/__init__.py``（stdio_client）、``mcp/client/sse.py``、
    ``mcp/client/streamable_http.py``。这里不启动子任务：scope 的宿主 task
    归属与嵌套关系与真实实现完全一致，子任务只影响关闭耗时，不影响断言。
    """
    async with anyio.create_task_group():
        yield


async def _capture(scenario: Callable[[], Awaitable[None]]) -> BaseException | None:
    """在独立 task 中执行场景，返回其抛出的异常（正常结束返回 None）。

    独立 task 是刻意为之：被污染的 cancel scope 会随该 task 一起消亡，不会污染
    后续用例——这正对应真实世界里「一个被泄漏的 owner task」。

    Args:
        scenario: 无参协程函数，内部自行搭建 task group 场景。

    Returns:
        场景抛出的异常；正常结束返回 None。
    """
    task = asyncio.get_running_loop().create_task(scenario())
    try:
        await task
    except BaseException as exc:
        # 探针需要观察任意异常类型（含 BaseExceptionGroup）；此处是测试装置，
        # 捕获后作为返回值交给断言，不存在吞异常。
        return exc
    return None


@pytest.mark.asyncio
async def test_cross_task_close_raises() -> None:
    """约束 1：scope 必须由「进入它的那个 task」退出。

    这是线上「MCP 服务器 '...' 清理错误（可忽略）」的根因：连接在 boot/消息
    task 进入，关闭在信号守卫 task 执行。
    """

    async def _scenario() -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        await stack.enter_async_context(_fake_transport())
        # 另起一个 task 去关闭 → 跨 task
        await asyncio.get_running_loop().create_task(stack.aclose())

    exc = await _capture(_scenario)
    assert isinstance(exc, RuntimeError)
    assert "different task than it was entered in" in str(exc)


@pytest.mark.asyncio
async def test_fifo_close_within_one_task_raises() -> None:
    """约束 2：同一 task 内多 scope 严格嵌套，先关先进入的（FIFO）必失败。"""

    async def _scenario() -> None:
        cms = [_fake_transport(), _fake_transport()]
        await cms[0].__aenter__()
        await cms[1].__aenter__()
        await cms[0].__aexit__(None, None, None)

    exc = await _capture(_scenario)
    assert isinstance(exc, RuntimeError)
    assert "isn't the current tasks's current cancel scope" in str(exc)


@pytest.mark.asyncio
async def test_lifo_close_within_one_task_succeeds() -> None:
    """约束 2 反面：逆序退出无异常——这是每 server 一个 owner task 的天然形态。"""

    async def _scenario() -> None:
        cms = [_fake_transport(), _fake_transport()]
        await cms[0].__aenter__()
        await cms[1].__aenter__()
        await cms[1].__aexit__(None, None, None)
        await cms[0].__aexit__(None, None, None)

    assert await _capture(_scenario) is None


@pytest.mark.asyncio
async def test_closing_middle_of_three_within_one_task_raises() -> None:
    """约束 3：单 task 内无法只关中间的 scope。

    推论：单 owner 管所有 server 时，「只重连某个非末尾 server」在结构上不可行
    （必须先退出其后进入的 server）——故采用每 server 一个 owner task。
    """

    async def _scenario() -> None:
        cms = [_fake_transport() for _ in range(3)]
        for cm in cms:
            await cm.__aenter__()
        await cms[1].__aexit__(None, None, None)

    exc = await _capture(_scenario)
    assert isinstance(exc, RuntimeError)
    assert "isn't the current tasks's current cancel scope" in str(exc)
