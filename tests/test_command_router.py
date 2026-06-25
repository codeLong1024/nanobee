"""Slash Command 系统测试 — CommandRouter 命令检测、路由分发、内核集成。"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nanobee.agent.messages import InboundMessage, OutboundMessage
from nanobee.kernel.command_router import CommandContext, CommandRouter


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def router() -> CommandRouter:
    """创建全新的 CommandRouter 实例（含内置命令）。"""
    return CommandRouter()


@pytest.fixture
def sample_msg() -> InboundMessage:
    """创建标准测试用 InboundMessage。"""
    return InboundMessage(
        channel="test",
        sender_id="test_user",
        chat_id="test_chat",
        content="hello",
    )


def _make_mock_kernel() -> MagicMock:
    """构造一个最小化的 mock kernel，用于 CommandRouter 集成测试。

    绕过 __init__ 避免需要完整的 Config/PluginManager 等依赖。
    """
    from nanobee.kernel.kernel import NanobeeKernel

    kernel = NanobeeKernel.__new__(NanobeeKernel)
    kernel.event_bus = MagicMock()
    kernel.plugin_manager = MagicMock()
    kernel.session_manager = MagicMock()
    kernel._agent_loop = MagicMock()
    kernel._active_turns: dict[str, asyncio.Task] = {}
    kernel._booted = True
    kernel.config = MagicMock()
    kernel.command_router = CommandRouter()
    return kernel


# ═══════════════════════════════════════════════════════════════════════
# 命令检测与路由
# ═══════════════════════════════════════════════════════════════════════


class TestCommandDetection:
    """测试命令检测与路由逻辑。"""

    @pytest.mark.asyncio
    async def test_non_command_returns_none(self, router, sample_msg):
        """非 / 开头消息应返回 None，走正常 Agent 流程。"""
        ctx = CommandContext(msg=sample_msg, kernel=_make_mock_kernel())
        sample_msg.content = "hello world"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None, "普通消息不应被识别为命令"

    @pytest.mark.asyncio
    async def test_slash_prefix_detected(self, router, sample_msg):
        """以 / 开头的消息应被识别为命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/help"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None, "/help 应被路由到处理器"

    @pytest.mark.asyncio
    async def test_whitespace_before_slash_still_detected(self, router, sample_msg):
        """消息开头的空白字符应在 trim 后仍识别为命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "  /help"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None, "带前导空格的 /help 应被识别"

    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, router, sample_msg):
        """未知命令（不在注册表中）应返回 None，透传给 Agent。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/nonexistent_command"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None, "未知命令应透传给 Agent"


class TestBuiltinCommands:
    """测试内置命令处理器。"""

    @pytest.mark.asyncio
    async def test_help_lists_commands(self, router, sample_msg):
        """/help 应列出所有已注册命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/help"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert isinstance(result, OutboundMessage)
        assert "/stop" in result.content
        assert "/new" in result.content
        assert "/status" in result.content
        assert "/help" in result.content

    @pytest.mark.asyncio
    async def test_help_includes_custom_commands(self, router, sample_msg):
        """/help 应包含插件注册的自定义命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        async def _dummy_handler(c, a):
            return OutboundMessage(channel="test", chat_id="test", content="ok")

        router.register("/mytask", _dummy_handler, "我的自定义任务")
        sample_msg.content = "/help"
        result = await router.dispatch(sample_msg.content, ctx)
        assert "/mytask" in result.content
        assert "我的自定义任务" in result.content

    @pytest.mark.asyncio
    async def test_new_clears_session(self, router, sample_msg):
        """/new 应清空 session 消息并持久化，不删除会话文件。"""
        kernel = _make_mock_kernel()
        # Mock session with messages
        mock_session = MagicMock()
        mock_session.messages = [{"role": "user", "content": "old msg"}]
        kernel.session_manager.get_or_create.return_value = mock_session

        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/new"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "会话已重置" in result.content
        # 验证正确的方法调用链：get → clear → save → invalidate
        kernel.session_manager.get_or_create.assert_called_once()
        kernel.session_manager.save.assert_called_once_with(mock_session)
        kernel.session_manager.invalidate.assert_called_once()
        # 不应删除文件
        kernel.session_manager.delete.assert_not_called()
        # 验证消息已被清空
        assert mock_session.messages == []

    @pytest.mark.asyncio
    async def test_new_uses_correct_user_and_session_ids(self, router, sample_msg):
        """/new 应使用 sender_id 作为 user_id，session_id 作为会话标识。"""
        kernel = _make_mock_kernel()
        # Mock session with messages
        mock_session = MagicMock()
        mock_session.messages = [{"role": "user", "content": "test"}]
        kernel.session_manager.get_or_create.return_value = mock_session

        sample_msg.channel = "dingtalk"
        sample_msg.sender_id = "staff_12345"
        sample_msg.chat_id = "group_abc"
        sample_msg.content = "/new"
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        await router.dispatch(sample_msg.content, ctx)

        # session_id 应为 channel:chat_id
        expected_session_id = sample_msg.session_id  # "dingtalk:group_abc"
        kernel.session_manager.get_or_create.assert_called_once_with(
            "staff_12345", expected_session_id
        )
        kernel.session_manager.save.assert_called_once_with(mock_session)
        kernel.session_manager.invalidate.assert_called_once_with(
            "staff_12345", expected_session_id
        )

    @pytest.mark.asyncio
    async def test_status_shows_session_info(self, router, sample_msg):
        """/status 应展示会话消息数和 turn 状态。"""
        kernel = _make_mock_kernel()

        # Mock session with messages
        mock_session = MagicMock()
        mock_session.messages = ["msg1", "msg2", "msg3"]
        kernel.session_manager.get_or_create.return_value = mock_session

        # Mock lock manager
        mock_lock_mgr = MagicMock()
        mock_lock_mgr.current_locks.return_value = ["user_a", "user_b"]
        kernel._agent_loop._lock_manager = mock_lock_mgr

        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/status"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "消息数: 3" in result.content
        assert "当前状态: 空闲等待" in result.content
        assert "user_a" in result.content

    @pytest.mark.asyncio
    async def test_status_shows_running_when_turn_active(self, router, sample_msg):
        """/status 在有活跃 turn 时应显示"运行中"。"""
        kernel = _make_mock_kernel()

        mock_session = MagicMock()
        mock_session.messages = []
        kernel.session_manager.get_or_create.return_value = mock_session

        # 模拟活跃 turn：创建一个未完成的 asyncio.Task
        async def _fake_work():
            await asyncio.sleep(10)

        active_task = asyncio.create_task(_fake_work())
        key = sample_msg.context_id
        kernel._active_turns[key] = active_task

        mock_lock_mgr = MagicMock()
        mock_lock_mgr.current_locks.return_value = []
        kernel._agent_loop._lock_manager = mock_lock_mgr

        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/status"
        result = await router.dispatch(sample_msg.content, ctx)
        assert "当前状态: 正在处理" in result.content

        # 清理
        active_task.cancel()
        try:
            await active_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stop_when_no_active_turn(self, router, sample_msg):
        """/stop 在无活跃 turn 时应提示无任务。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/stop"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "没有正在运行" in result.content

    @pytest.mark.asyncio
    async def test_stop_cancels_active_turn(self, router, sample_msg):
        """/stop 应对活跃 turn 调用 task.cancel()。"""
        kernel = _make_mock_kernel()

        # 创建一个可被取消的 mock task
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        key = sample_msg.context_id
        kernel._active_turns[key] = mock_task

        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/stop"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "已发送停止信号" in result.content
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_ignores_completed_task(self, router, sample_msg):
        """/stop 对已完成的任务不应调用 cancel。"""
        kernel = _make_mock_kernel()

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        key = sample_msg.context_id
        kernel._active_turns[key] = mock_task

        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/stop"
        result = await router.dispatch(sample_msg.content, ctx)
        assert "没有正在运行" in result.content
        mock_task.cancel.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# 命令注册与扩展
# ═══════════════════════════════════════════════════════════════════════


class TestCommandRegistration:
    """测试命令注册/注销和插件扩展机制。"""

    @pytest.mark.asyncio
    async def test_register_custom_command(self, router, sample_msg):
        """插件可以注册自定义命令并正确执行。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        async def _my_handler(c, a):
            return OutboundMessage(
                channel=c.msg.channel,
                chat_id=c.msg.chat_id,
                content="custom response",
            )

        router.register("/mytask", _my_handler, "自定义任务")
        sample_msg.content = "/mytask"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert result.content == "custom response"

    @pytest.mark.asyncio
    async def test_register_rejects_non_slash_name(self, router):
        """注册不以 / 开头的命令名应抛出 ValueError。"""

        async def _dummy(c, a):
            return OutboundMessage(channel="t", chat_id="t", content="ok")

        with pytest.raises(ValueError, match="必须以 / 开头"):
            router.register("badname", _dummy)

    @pytest.mark.asyncio
    async def test_unregister_removes_command(self, router, sample_msg):
        """注销命令后应无法再匹配。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        async def _dummy(c, a):
            return OutboundMessage(channel="t", chat_id="t", content="ok")

        router.register("/remove_me", _dummy)
        assert "/remove_me" in router.commands

        router.unregister("/remove_me")
        assert "/remove_me" not in router.commands

        # 注销后 dispatch 应返回 None（透传）
        sample_msg.content = "/remove_me"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_override_builtin_command(self, router, sample_msg):
        """重新注册同名命令应覆盖内置处理器。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        async def _custom_stop(c, a):
            return OutboundMessage(
                channel=c.msg.channel,
                chat_id=c.msg.chat_id,
                content="custom stop message",
            )

        router.register("/stop", _custom_stop, "自定义停止")
        sample_msg.content = "/stop"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result.content == "custom stop message"

    @pytest.mark.asyncio
    async def test_handler_receives_args(self, router, sample_msg):
        """命令处理器应接收到解析后的参数列表。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        captured_args = None

        async def _arg_handler(c, a):
            nonlocal captured_args
            captured_args = a
            return OutboundMessage(channel="t", chat_id="t", content="ok")

        router.register("/args", _arg_handler)
        sample_msg.content = "/args foo bar baz"
        await router.dispatch(sample_msg.content, ctx)
        assert captured_args == ["foo", "bar", "baz"]

    @pytest.mark.asyncio
    async def test_handler_receives_empty_args(self, router, sample_msg):
        """无参数命令应收到空列表。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        captured_args = None

        async def _arg_handler(c, a):
            nonlocal captured_args
            captured_args = a
            return OutboundMessage(channel="t", chat_id="t", content="ok")

        router.register("/noargs", _arg_handler)
        sample_msg.content = "/noargs"
        await router.dispatch(sample_msg.content, ctx)
        assert captured_args == []


# ═══════════════════════════════════════════════════════════════════════
# 错误处理与边界条件
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """测试命令执行异常和边界情况。"""

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error_message(self, router, sample_msg):
        """命令处理器抛异常时，应返回错误 OutboundMessage 而非崩溃。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)

        async def _broken_handler(c, a):
            raise RuntimeError("模拟命令执行失败")

        router.register("/broken", _broken_handler)
        sample_msg.content = "/broken"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "执行失败" in result.content

    @pytest.mark.asyncio
    async def test_empty_message_not_command(self, router, sample_msg):
        """空消息不应被识别为命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = ""
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_not_command(self, router, sample_msg):
        """仅含空白字符的消息不应被识别为命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "   "
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_slash_in_middle_not_command(self, router, sample_msg):
        """消息中间包含 / 不应被识别为命令。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "hello /help world"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is None, "仅开头 / 才应触发命令检测"

    @pytest.mark.asyncio
    async def test_multiple_slashes_extracts_first_word(self, router, sample_msg):
        """/命令 包含额外的 / 时，应正确提取命令名。"""
        kernel = _make_mock_kernel()
        ctx = CommandContext(msg=sample_msg, kernel=kernel)
        sample_msg.content = "/new /something /else"
        result = await router.dispatch(sample_msg.content, ctx)
        assert result is not None
        assert "会话已重置" in result.content

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_no_error(self, router):
        """注销不存在的命令不应报错。"""
        router.unregister("/nonexistent")

    @pytest.mark.asyncio
    async def test_commands_property_returns_copy(self, router):
        """commands 属性应返回副本，外部修改不影响内部。"""
        orig = router.commands
        orig["/fake"] = lambda: None  # type: ignore
        assert "/fake" not in router.commands

    @pytest.mark.asyncio
    async def test_unregister_idempotent(self, router):
        """重复注销同一命令不应报错。"""
        async def _dummy(c, a):
            return OutboundMessage(channel="t", chat_id="t", content="ok")

        router.register("/temp", _dummy)
        router.unregister("/temp")
        router.unregister("/temp")  # 第二次不应报错
        assert "/temp" not in router.commands


# ═══════════════════════════════════════════════════════════════════════
# Kernel 集成测试
# ═══════════════════════════════════════════════════════════════════════


class _AsyncContextManagerMock(MagicMock):
    """支持 async with 的 MagicMock。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestKernelIntegration:
    """测试 CommandRouter 在 Kernel._handle_message_impl 中的集成。"""

    def _make_mock_agent(self):
        """创建带完整 mock 的 AgentLoop。

        需要 mock:
        - _connect_mcp: 异步方法（kernel._handle_message_impl 中调用）
        - dispatch: 公开消息入口（替代 _process_message + lock + queue）
        - try_inject: 中轮注入（替代 _pending_queues 直接访问）
        """
        mock_agent = MagicMock()
        mock_agent._connect_mcp = AsyncMock()
        mock_agent.dispatch = AsyncMock()
        mock_agent.try_inject = MagicMock(return_value=False)
        return mock_agent

    @pytest.mark.asyncio
    async def test_command_intercepted_before_lock(self):
        """命令消息应在获取锁之前被拦截，不进入 Agent Loop。"""
        kernel = _make_mock_kernel()

        mock_agent = self._make_mock_agent()
        kernel._agent_loop = mock_agent

        result = await kernel._handle_message_impl(
            message="/help",
            context_id="test_user",
            channel="test",
            sender_id="test_user",
        )
        assert result is not None
        assert "/stop" in result.content
        # 不应进入 Agent Loop
        mock_agent.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_command_enters_agent_loop(self):
        """非命令消息应进入 Agent Loop 正常处理。"""
        kernel = _make_mock_kernel()

        mock_agent = self._make_mock_agent()
        mock_agent.dispatch.return_value = OutboundMessage(
            channel="test", chat_id="test_user", content="agent reply"
        )
        kernel._agent_loop = mock_agent

        result = await kernel._handle_message_impl(
            message="hello world",
            context_id="test_user",
            channel="test",
            sender_id="test_user",
        )
        assert result is not None
        assert result.content == "agent reply"
        mock_agent.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_cancels_running_turn_in_kernel(self):
        """/stop 应通过 _active_turns 取消运行中的 turn。"""
        kernel = _make_mock_kernel()

        # 创建一个真实的 asyncio.Task 作为"运行中的 turn"
        async def _long_running():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise

        active_task = asyncio.create_task(_long_running())
        kernel._active_turns["test_user"] = active_task

        mock_agent = self._make_mock_agent()
        kernel._agent_loop = mock_agent

        # 发送 /stop 命令
        result = await kernel._handle_message_impl(
            message="/stop",
            context_id="test_user",
            channel="test",
            sender_id="test_user",
        )
        assert result is not None
        assert "已发送停止信号" in result.content

        # 等待取消生效
        await asyncio.sleep(0.05)
        assert active_task.cancelled(), "任务应该已被取消"

    @pytest.mark.asyncio
    async def test_kernel_command_router_initialized(self):
        """真实 mock kernel 应初始化 command_router。"""
        kernel = _make_mock_kernel()
        assert kernel.command_router is not None
        assert "/stop" in kernel.command_router.commands
        assert "/new" in kernel.command_router.commands
        assert "/status" in kernel.command_router.commands
        assert "/help" in kernel.command_router.commands


# ═══════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════


class AsyncMock(MagicMock):
    """支持 await 的 MagicMock 变体。"""

    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
