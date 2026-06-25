"""命令路由系统 — 处理以 / 开头的用户控制命令。

命令在进入 Agent Loop 之前被拦截，零 token 消耗。
遵循框架无知论：CommandRouter 提供注册机制，不做策略决策。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from nanobee.agent.messages import InboundMessage, OutboundMessage
    from nanobee.kernel.kernel import NanobeeKernel

from nanobee.utils.logger import logger
from nanobee.utils.notifications import build_notification, get_notification_content

# 命令处理器类型别名：异步函数，接收 (CommandContext, list[str]) 返回 OutboundMessage
CommandHandler = Callable[..., Awaitable["OutboundMessage"]]


@dataclass
class CommandContext:
    """命令执行上下文，仅包含必要信息，遵循最小权限原则。

    Attributes:
        msg: 入站消息（含 channel、sender_id、chat_id、session_id 等）。
        kernel: 内核实例（用于访问 session、lock 等核心资源）。
    """

    msg: "InboundMessage"
    kernel: "NanobeeKernel"


class CommandRouter:
    """命令路由系统。

    以 / 开头的消息识别为命令，路由到对应处理器。
    非命令消息返回 None，走正常 Agent 流程。

    内置命令：
    - /stop: 取消当前 Agent turn
    - /new: 重置当前会话
    - /status: 显示运行时状态
    - /help: 列出可用命令

    扩展方式：插件通过 kernel.command_router.register(name, handler) 注册自定义命令。
    """

    def __init__(self) -> None:
        """初始化命令路由器，注册内置命令处理器。"""
        self._commands: dict[str, CommandHandler] = {}
        self._descriptions: dict[str, str] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """注册内置命令处理器。"""
        self.register("/stop", self._cmd_stop, "取消当前正在运行的 Agent 任务")
        self.register("/new", self._cmd_new, "重置当前会话，开始全新对话")
        self.register("/status", self._cmd_status, "显示运行时状态（消息数、当前状态等）")
        self.register("/help", self._cmd_help, "显示此帮助信息")

    # ── 公共接口 ──────────────────────────────────────────

    def register(self, name: str, handler: CommandHandler, description: str = "") -> None:
        """注册自定义命令处理器。

        Args:
            name: 命令名（如 "/mytask"），必须以 / 开头。
            handler: 异步命令处理器，签名为 async (ctx: CommandContext, args: list[str]) -> OutboundMessage。
            description: 命令描述，用于 /help 展示。插件命令建议提供中文描述。

        Raises:
            ValueError: 命令名不以 / 开头。
        """
        if not name.startswith("/"):
            raise ValueError(f"命令名必须以 / 开头: {name}")
        self._commands[name] = handler
        self._descriptions[name] = description
        logger.debug("已注册命令: {}", name)

    def unregister(self, name: str) -> None:
        """注销命令。

        Args:
            name: 命令名。
        """
        self._commands.pop(name, None)
        self._descriptions.pop(name, None)

    @property
    def commands(self) -> dict[str, CommandHandler]:
        """返回所有已注册命令的只读视图。"""
        return dict(self._commands)

    async def dispatch(self, text: str, ctx: CommandContext) -> "OutboundMessage | None":
        """检测并分发命令。

        若 text 以 / 开头，提取命令名并路由到处理器。
        否则返回 None，消息走正常 Agent 流程。

        Args:
            text: 用户消息文本（已 trim 过的原始文本）。
            ctx: 命令执行上下文。

        Returns:
            命令处理结果（OutboundMessage），非命令或未知命令返回 None。
        """
        text = text.strip()
        if not text.startswith("/"):
            return None

        # 提取命令名和参数："/stop now" → cmd="/stop", args=["now"]
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        raw_args = parts[1] if len(parts) > 1 else ""
        args = raw_args.split() if raw_args else []

        handler = self._commands.get(cmd)
        if handler is None:
            # 未知命令：透传给 Agent（LLM 可能将其理解为自然语言）
            return None

        logger.info("执行命令: {} (sender={})", cmd, ctx.msg.sender_id)
        try:
            return await handler(ctx, args)
        except Exception:
            logger.exception("命令 {} 执行失败", cmd)
            return build_notification(
                "command_failed",
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                cmd=cmd,
            )

    # ── 内置命令处理器 ────────────────────────────────────

    async def _cmd_stop(self, ctx: CommandContext, args: list[str]) -> "OutboundMessage":
        """取消当前正在运行的 Agent turn。

        通过 kernel._active_turns 查找运行中的 Task 并调用 cancel()。
        CancelledError 沿现有取消路径传播（runner.py → loop.py → kernel.py），
        _handle_message_impl 的 except CancelledError 块捕获后返回友好消息。
        """
        key = ctx.msg.context_id
        active_turns: dict[str, asyncio.Task] = getattr(ctx.kernel, "_active_turns", {})
        task = active_turns.get(key)

        if task is None or task.done():
            return build_notification(
                "command_stop_idle",
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
            )

        task.cancel()
        logger.info("已发送取消信号到 turn: {}", key)
        return build_notification(
            "command_stop",
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
        )

    async def _cmd_new(self, ctx: CommandContext, args: list[str]) -> "OutboundMessage":
        """重置当前会话。

        先取消活跃 turn（如有），然后清空 session 消息列表并持久化。
        不删除 JSONL 文件，保留 session 元数据和 .consolidation.jsonl 归档记录。
        """
        user_id = ctx.msg.sender_id
        session_id = ctx.msg.session_id

        # 1. 取消活跃 turn（如有）
        key = ctx.msg.context_id
        active_turns: dict[str, asyncio.Task] = getattr(ctx.kernel, "_active_turns", {})
        task = active_turns.get(key)
        if task and not task.done():
            task.cancel()
            logger.info("已发送取消信号到 turn: {} (via /new)", key)

        # 2. 获取 session，清空消息，持久化
        session = ctx.kernel.session_manager.get_or_create(user_id, session_id)
        before_count = len(session.messages)
        session.messages.clear()
        ctx.kernel.session_manager.save(session)
        ctx.kernel.session_manager.invalidate(user_id, session_id)

        logger.info(
            "已重置会话: user={} session={} (清空 {} 条消息，保留归档)",
            user_id, session_id, before_count,
        )
        return build_notification(
            "command_new",
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
        )

    async def _cmd_status(self, ctx: CommandContext, args: list[str]) -> "OutboundMessage":
        """显示当前运行时状态。

        展示信息：用户 ID、会话 ID、消息数、turn 运行状态、当前锁定用户列表。
        """
        user_id = ctx.msg.sender_id
        session_id = ctx.msg.session_id
        key = ctx.msg.context_id

        # 获取 session 消息数
        session = ctx.kernel.session_manager.get_or_create(user_id, session_id)
        msg_count = len(session.messages)

        # 检查是否有活跃 turn
        active_turns: dict[str, asyncio.Task] = getattr(ctx.kernel, "_active_turns", {})
        task = active_turns.get(key)
        current_status = "正在处理" if (task and not task.done()) else "空闲等待"

        # 当前锁状态
        locked_users: list[str] = []
        if ctx.kernel._agent_loop is not None:
            lock_mgr = ctx.kernel._agent_loop._lock_manager
            locked_users = lock_mgr.current_locks()

        return build_notification(
            "command_status",
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            user_id=user_id,
            session_id=session_id,
            msg_count=msg_count,
            turn_status=current_status,
            locked_users=", ".join(locked_users) if locked_users else "无",
        )

    async def _cmd_help(self, ctx: CommandContext, args: list[str]) -> "OutboundMessage":
        """列出所有可用命令及描述。"""
        lines = []
        for cmd in sorted(self._commands.keys()):
            desc = self._descriptions.get(cmd, "（自定义命令）")
            lines.append(f"- `{cmd}`: {desc}")

        return build_notification(
            "command_help",
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            command_list="\n".join(lines),
        )
