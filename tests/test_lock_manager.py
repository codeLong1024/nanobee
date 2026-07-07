"""
LockManager 单元测试
"""

from __future__ import annotations

import asyncio

import pytest

from nanobee.kernel.lock_manager import LockManager


@pytest.mark.asyncio
async def test_same_user_serial():
    """同一用户的任务必须串行执行"""
    lock_mgr = LockManager()
    events: list[int] = []

    async def task(n: int) -> None:
        async with lock_mgr.acquire("user-a"):
            events.append(n)
            await asyncio.sleep(0.01)

    await asyncio.gather(task(1), task(2))
    assert events == [1, 2], f"期望串行 [1, 2]，实际 {events}"


@pytest.mark.asyncio
async def test_different_users_parallel():
    """不同用户的任务可以并行执行"""
    lock_mgr = LockManager()
    events: list[str] = []

    async def task(user: str, label: str) -> None:
        async with lock_mgr.acquire(user):
            events.append(label)
            await asyncio.sleep(0.01)

    await asyncio.gather(task("user-a", "A"), task("user-b", "B"))
    assert "A" in events
    assert "B" in events


@pytest.mark.asyncio
async def test_three_users_interleaving():
    """三个用户并发，同一用户串行，不同用户可交错"""
    lock_mgr = LockManager()
    order: list[str] = []

    async def task_a(n: int) -> None:
        async with lock_mgr.acquire("a"):
            order.append(f"a{n}_enter")
            await asyncio.sleep(0.02)
            order.append(f"a{n}_exit")

    async def task_b(n: int) -> None:
        async with lock_mgr.acquire("b"):
            order.append(f"b{n}_enter")
            await asyncio.sleep(0.01)
            order.append(f"b{n}_exit")

    await asyncio.gather(task_a(1), task_b(1), task_a(2), task_b(2))

    # a1 必须在 a2 之前完成
    a_events = [e for e in order if e.startswith("a")]
    assert a_events == ["a1_enter", "a1_exit", "a2_enter", "a2_exit"]

    # b1 必须在 b2 之前完成
    b_events = [e for e in order if e.startswith("b")]
    assert b_events == ["b1_enter", "b1_exit", "b2_enter", "b2_exit"]


@pytest.mark.asyncio
async def test_concurrent_gate():
    """全局并发门控生效"""
    lock_mgr = LockManager(max_concurrent=2)
    running = 0
    max_running = 0

    async def task(user: str) -> None:
        nonlocal running, max_running
        async with lock_mgr.acquire(user):
            running += 1
            max_running = max(max_running, running)
            await asyncio.sleep(0.02)
            running -= 1

    # 启动 5 个不同用户的任务
    await asyncio.gather(*[task(f"user-{i}") for i in range(5)])
    assert max_running <= 2, f"全局门控限制为 2，实际最大并发 {max_running}"


@pytest.mark.asyncio
async def test_current_locks():
    """current_locks 返回持有锁的用户"""
    lock_mgr = LockManager()
    acquired: list[str] = []

    async def hold(user: str, duration: float) -> None:
        async with lock_mgr.acquire(user):
            acquired.append(user)
            await asyncio.sleep(duration)

    task_a = asyncio.create_task(hold("a", 0.05))
    await asyncio.sleep(0.01)
    assert "a" in lock_mgr.current_locks()

    await task_a
    assert "a" not in lock_mgr.current_locks()


@pytest.mark.asyncio
async def test_clear():
    """清空锁管理器"""
    lock_mgr = LockManager()
    async with lock_mgr.acquire("user-a"):
        pass
    lock_mgr.clear()
    assert lock_mgr.active_users == 0


@pytest.mark.asyncio
async def test_per_user_concurrency_with_ordering():
    """多用户并发：同用户按序执行（start/end 顺序可验证），不同用户可交错。"""
    lock_mgr = LockManager()
    order: list[str] = []

    async def send(user: str, msg_id: str) -> None:
        async with lock_mgr.acquire(user):
            order.append(f"{user}_{msg_id}_start")
            await asyncio.sleep(0.02)
            order.append(f"{user}_{msg_id}_end")

    await asyncio.gather(
        send("user-alice", "1"),
        send("user-bob", "1"),
        send("user-alice", "2"),
        send("user-bob", "2"),
    )

    # 同一用户严格串行
    alice_events = [e for e in order if e.startswith("user-alice")]
    assert alice_events == [
        "user-alice_1_start", "user-alice_1_end",
        "user-alice_2_start", "user-alice_2_end",
    ]
    bob_events = [e for e in order if e.startswith("user-bob")]
    assert bob_events == [
        "user-bob_1_start", "user-bob_1_end",
        "user-bob_2_start", "user-bob_2_end",
    ]
    # 不同用户可交错
    assert "user-bob_1_start" in order
    assert "user-bob_1_end" in order
