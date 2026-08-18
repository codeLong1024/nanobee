"""声明式无历史机制（fresh_session）测试。

验证框架层 fresh_session 机制：
1. InboundMessage.fresh_session 默认为 False（不改变现有调用方行为）
2. AgentLoop._resolve_session_id 在 fresh_session=True 时返回独立隔离会话 ID，
   False 时返回原会话 ID
3. _state_save 在 fresh_session=True 的 turn 结束后回收一次性会话
   （清理磁盘文件与缓存，防止孤儿会话累积）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from nanobee.agent.loop import AgentLoop, TurnContext, TurnState
from nanobee.agent.messages import InboundMessage
from nanobee.session.session_manager import SessionManager


def _make_ctx(fresh_session: bool) -> TurnContext:
    """构造最小 TurnContext 用于 _resolve_session_id 测试。"""
    msg = InboundMessage(
        channel="dingtalk",
        sender_id="user_a",
        chat_id="chat_a",
        content="测试消息",
        fresh_session=fresh_session,
    )
    return TurnContext(
        msg=msg,
        context_id="user_a",
        session_id="dingtalk:chat_a",
        state=TurnState.BUILD,
        turn_id="user_a:123456",
    )


def _make_save_ctx(fresh_session: bool, turn_id: str = "user_a:123456") -> TurnContext:
    """构造可驱动 _state_save 的 TurnContext。

    补充 _state_save 所需的 final_content / turn_wall_started_at 字段。
    """
    ctx = _make_ctx(fresh_session)
    ctx.turn_id = turn_id
    ctx.final_content = "这是一条回复"
    ctx.turn_wall_started_at = time.time()
    return ctx


def _run_save(tmp_path: Path, ctx: TurnContext) -> SessionManager:
    """用最小化 AgentLoop（object.__new__ 绕过重型构造器）执行 _state_save。"""
    loop = object.__new__(AgentLoop)
    loop.session_manager = SessionManager(tmp_path / "users")
    loop.event_bus = None
    asyncio.run(loop._state_save(ctx))
    return loop.session_manager


class TestStateSaveFreshSessionCleanup:
    """_state_save 对一次性 fresh 会话的回收测试。"""

    def test_fresh_session_removed_after_save(self, tmp_path: Path) -> None:
        """fresh_session=True 时，turn 结束后一次性会话应从磁盘与缓存删除。"""
        ctx = _make_save_ctx(fresh_session=True)
        mgr = _run_save(tmp_path, ctx)

        loop = object.__new__(AgentLoop)
        resolved = loop._resolve_session_id(ctx)
        # 缓存中不应残留 fresh 会话
        assert (ctx.context_id, resolved) not in mgr._cache
        # 磁盘上不应残留 fresh 会话文件
        assert mgr.store.load(ctx.context_id, resolved) is None

    def test_normal_session_persisted_after_save(self, tmp_path: Path) -> None:
        """fresh_session=False 时，会话应正常保存（不删除）。"""
        ctx = _make_save_ctx(fresh_session=False)
        mgr = _run_save(tmp_path, ctx)

        session = mgr.get_or_create(ctx.context_id, ctx.session_id)
        # 回复应已写入原会话
        contents = [m.get("content") for m in session.messages]
        assert ctx.final_content in contents

    def test_fresh_session_does_not_pollute_main_session(self, tmp_path: Path) -> None:
        """fresh 会话回复不应写入用户主会话。"""
        ctx = _make_save_ctx(fresh_session=True)
        mgr = _run_save(tmp_path, ctx)

        main = mgr.get_or_create(ctx.context_id, ctx.session_id)
        assert len(main.messages) == 0

    def test_fresh_session_reclaimed_even_on_exception(self, tmp_path: Path) -> None:
        """event_bus.publish 抛异常时，fresh 会话仍必须被回收（try/finally 保证）。

        覆盖 P0-2 修复：save 成功后、delete 之前若发生异常，fresh 会话不应残留为孤儿。
        """
        ctx = _make_save_ctx(fresh_session=True)

        # 用最小化 AgentLoop + 抛异常的 event_bus 触发 try 块中异常
        loop = object.__new__(AgentLoop)
        loop.session_manager = SessionManager(tmp_path / "users")

        class _RaisingBus:
            async def publish(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("event bus boom")

        loop.event_bus = _RaisingBus()

        # _state_save 应抛 RuntimeError，但 finally 必须先回收 fresh 会话
        with pytest.raises(RuntimeError, match="event bus boom"):
            asyncio.run(loop._state_save(ctx))

        resolved = loop._resolve_session_id(ctx)
        # finally 执行后，缓存和磁盘都不应残留 fresh 会话
        assert (ctx.context_id, resolved) not in loop.session_manager._cache
        assert loop.session_manager.store.load(ctx.context_id, resolved) is None


class TestInboundMessageFreshSessionDefault:
    """InboundMessage.fresh_session 字段默认值测试。"""

    def test_fresh_session_defaults_to_false(self) -> None:
        """默认应为 False，保证现有调用方行为不变。"""
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="hello",
        )
        assert msg.fresh_session is False

    def test_fresh_session_can_be_set_true(self) -> None:
        """可显式声明为 True。"""
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="hello",
            fresh_session=True,
        )
        assert msg.fresh_session is True


class TestResolveSessionId:
    """AgentLoop._resolve_session_id 隔离会话推导测试。"""

    def test_fresh_session_returns_isolated_id(self) -> None:
        """fresh_session=True 时返回独立隔离会话 ID。"""
        ctx = _make_ctx(fresh_session=True)
        loop = object.__new__(AgentLoop)
        resolved = loop._resolve_session_id(ctx)
        assert resolved != ctx.session_id
        assert resolved.startswith(AgentLoop._FRESH_SESSION_PREFIX)
        # 包含原会话 ID 与 turn_id，保证可追溯且每次触发唯一
        assert ctx.session_id in resolved
        assert ctx.turn_id in resolved

    def test_fresh_session_id_unique_per_turn(self) -> None:
        """不同 turn_id 的隔离会话 ID 应互不相同（不残留历史）。"""
        ctx_a = _make_ctx(fresh_session=True)
        ctx_b = _make_ctx(fresh_session=True)
        ctx_b.turn_id = "user_a:999999"

        loop = object.__new__(AgentLoop)
        resolved_a = loop._resolve_session_id(ctx_a)
        resolved_b = loop._resolve_session_id(ctx_b)

        assert resolved_a != resolved_b

    def test_normal_session_returns_original_id(self) -> None:
        """fresh_session=False 时原样返回原会话 ID。"""
        ctx = _make_ctx(fresh_session=False)
        loop = object.__new__(AgentLoop)
        resolved = loop._resolve_session_id(ctx)
        assert resolved == ctx.session_id
