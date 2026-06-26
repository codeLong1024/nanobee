"""Tool Cron 插件 — 定时任务工具（add, list, remove）。

基于 nanobot/agent/tools/cron.py 适配 nanobee 插件架构。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nanobee.builtin.tool_cron.service import CronService
from nanobee.builtin.tool_cron.types import CronJob, CronSchedule
from nanobee.kernel.context_sandbox_var import current_request_context
from nanobee.plugins import ToolPlugin

from nanobee.utils.logger import logger


class ToolCronPlugin(ToolPlugin):
    """Cron 定时任务工具插件。

    提供 cron 工具的三个操作（add / list / remove）。
    通过 CURRENT_REQUEST_CONTEXT ContextVar 按 turn 获取会话信息，
    替代旧版 set_context() 实例属性写入模式。
    """

    name = "tool_cron"
    version = "1.0.0"
    plugin_type = "tool"

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)
        self._cron: CronService | None = None
        self._default_timezone: str = "UTC"

    def initialize(self, kernel: Any) -> None:
        """初始化插件：创建 CronService 实例。"""
        super().initialize(kernel)

        self._default_timezone = self.get_config("default_timezone", "UTC")

        # 存储路径延迟初始化 — 在 execute_tool 中基于 context_root 确定
        self._cron_base_dir: Path | None = None
        self._current_store_path: Path | None = None

        logger.info("Cron 插件初始化完成")

    def on_enable(self) -> None:
        """启用时启动 CronService。

        首次启动时扫描 users/*/cron/jobs.json 加载已有任务。
        """
        super().on_enable()
        if self._cron is None:
            self._scan_existing_jobs()
        if self._cron is not None:
            try:
                self._cron.start()
                self._enabled = True
            except RuntimeError as e:
                logger.error("Cron 服务启动失败: {}", e)

    def _scan_existing_jobs(self) -> None:
        """扫描 users 目录下已有的 cron 任务文件。"""
        if not self.kernel:
            return
        data_dir = Path(self.kernel.data_dir).expanduser()
        users_base = data_dir / "users"
        if not users_base.is_dir():
            return
        for user_dir in sorted(users_base.iterdir()):
            cron_file = user_dir / "cron" / "jobs.json"
            if cron_file.is_file():
                self._current_store_path = cron_file
                self._cron = CronService(
                    store_path=cron_file,
                    on_job=self._on_job_execute,
                    max_sleep_ms=300_000,
                )
                logger.info(
                    "Cron: 从 {} 加载现有任务", cron_file,
                )
                return

    def on_disable(self) -> None:
        """禁用时停止 CronService。"""
        if self._cron is not None:
            self._cron.stop()
        super().on_disable()

    def _resolve_store_path(self, context_id: str) -> Path:
        """根据 context_id 解析 cron 任务存储路径（线程安全，无实例属性依赖）。

        cron 数据需要持久化，放在 <context_root>/cron/ 下（与 skills/ 平级）。
        若 context_root 不可用，回退到 <data_dir>/cron/jobs_<context_id>.json。

        Args:
            context_id: 用户上下文 ID

        Returns:
            解析后的存储路径
        """
        if self.context_root is not None:
            store_path = self.context_root / "cron" / "jobs.json"
        else:
            if self._cron_base_dir is None:
                data_dir = Path(self._kernel.data_dir) if self._kernel and hasattr(self._kernel, "data_dir") else Path.cwd()
                self._cron_base_dir = data_dir / "cron"
                self._cron_base_dir.mkdir(parents=True, exist_ok=True)
            store_path = self._cron_base_dir / f"jobs_{context_id}.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return store_path

    def _ensure_cron_service(self, store_path: Path) -> None:
        """确保 CronService 已初始化并指向指定存储路径。

        Args:
            store_path: cron 任务存储路径
        """
        if self._cron is None or getattr(self._cron, "store_path", None) != store_path:
            if self._cron is not None:
                self._cron.stop()
            self._cron = CronService(
                store_path=store_path,
                on_job=self._on_job_execute,
                max_sleep_ms=300_000,
            )
            logger.info("Cron 服务存储路径: {}", store_path)

    def get_tools(self) -> list[dict[str, Any]]:
        """获取工具定义列表。

        Returns:
            包含 cron 工具的 OpenAI function schema 列表
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "cron",
                    "description": (
                        "Schedule reminders and recurring tasks. Actions: add, list, remove."
                        f" If tz is omitted, cron expressions and naive ISO times default to"
                        f" {self._default_timezone}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "list", "remove"],
                                "description": "Action to perform",
                            },
                            "name": {
                                "type": "string",
                                "description": "Optional short human-readable label for the job (e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message.",
                            },
                            "message": {
                                "type": "string",
                                "description": "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers (e.g., 'Send a reminder to WeChat' or 'Check system status and report'). Not used for action='list' or action='remove'.",
                            },
                            "every_seconds": {
                                "type": "integer",
                                "description": "Interval in seconds (for recurring tasks). Minimum: 1.",
                                "minimum": 1,
                            },
                            "cron_expr": {
                                "type": "string",
                                "description": "Cron expression like '0 9 * * *' (for scheduled tasks). Use tz parameter for timezone.",
                            },
                            "tz": {
                                "type": "string",
                                "description": "Optional IANA timezone for cron expressions (e.g. 'Asia/Shanghai'). When omitted with cron_expr, the tool's default timezone applies.",
                            },
                            "at": {
                                "type": "string",
                                "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). Naive values use the tool's default timezone. To specify a different timezone, include offset (e.g. '2026-02-12T10:30:00+08:00').",
                            },
                            "deliver": {
                                "type": "boolean",
                                "description": "Whether to deliver the execution result to the user channel (default true).",
                            },
                            "job_id": {
                                "type": "string",
                                "description": "REQUIRED when action='remove'. Job ID to remove (obtain via action='list').",
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行 cron 工具。

        Args:
            tool_name: 工具名称（固定为 "cron"）
            **kwargs: 工具参数

        Returns:
            执行结果字符串
        """
        if tool_name != "cron":
            raise ValueError(f"未知工具: {tool_name}")

        # 从 per-turn ContextVar 获取路由上下文（线程安全）
        rctx = current_request_context()
        if rctx is None:
            return "错误：无法获取当前会话上下文，无法执行 cron 操作"

        # 根据 context_id 解析存储路径并确保 CronService 已就绪
        store_path = self._resolve_store_path(rctx.context_id)
        self._ensure_cron_service(store_path)
        self._current_store_path = store_path

        action = kwargs.get("action", "").strip().lower()
        if action == "add":
            return self._add_job(
                channel=rctx.channel,
                chat_id=rctx.chat_id,
                context_metadata={} if rctx.context_id else {},
                session_key=rctx.session_id,
                user_id=rctx.context_id,
                **kwargs,
            )
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(**kwargs)
        return f"未知操作: {action}"

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        """校验 IANA 时区，无效时返回错误信息。"""
        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"错误：未知时区 '{tz}'"
        return None

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        """格式化时间戳为可读字符串。"""
        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    def _add_job(
        self,
        channel: str = "",
        chat_id: str = "",
        context_metadata: dict[str, Any] | None = None,
        session_key: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        """添加定时任务。

        Args:
            channel: 来源通道名
            chat_id: 会话/聊天 ID
            context_metadata: 通道附加元数据
            session_key: 会话键
            user_id: 用户唯一标识
        """
        if self._cron is None:
            return "错误：Cron 服务未初始化"

        # 确保服务已启动（懒加载模式：在首次添加任务时启动）
        if not getattr(self._cron, "_running", False):
            try:
                self._cron.start()
                logger.info("Cron 服务已启动（懒加载），存储路径: {}", self._current_store_path)
            except RuntimeError as e:
                logger.error("Cron 服务启动失败: {}", e)
                return f"错误：Cron 服务启动失败: {e}"

        message = kwargs.get("message", "")
        if not message:
            return (
                "错误：cron action='add' 需要非空 message 参数描述触发时执行的操作。"
                "请重试并包含 message=\"...\"。"
            )

        if not channel or not chat_id:
            return "错误：缺少会话上下文（channel/chat_id），无法创建投递任务"

        name = kwargs.get("name") or message[:30]
        every_seconds = kwargs.get("every_seconds")
        cron_expr = kwargs.get("cron_expr")
        tz = kwargs.get("tz")
        at = kwargs.get("at")
        deliver = kwargs.get("deliver", True)

        if tz and not cron_expr:
            return "错误：tz 参数只能与 cron_expr 一起使用"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"错误：无效的 ISO 时间格式 '{at}'。预期格式：YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "错误：需要 every_seconds、cron_expr 或 at 之一"

        job = self._cron.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=bool(deliver),
            channel=channel,
            to=chat_id,
            delete_after_run=delete_after,
            channel_meta=context_metadata or {},
            session_key=session_key or None,
            user_id=user_id or None,
        )
        return f"已创建任务 '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """将调度格式化为可读字符串。"""
        if schedule.kind == "cron":
            tz_str = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz_str}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"每 {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"每 {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"每 {ms // 1000}s"
            return f"每 {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """选择可读性最好的时区。"""
        return schedule.tz or self._default_timezone

    def _format_state(self, job: CronJob) -> list[str]:
        """格式化任务状态为显示行。"""
        lines: list[str] = []
        display_tz = self._display_timezone(job.schedule)
        state = job.state
        if state.last_run_at_ms:
            info = (
                f"  上次运行: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  下次运行: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream 记忆合并（长期记忆）。"
        return "系统内部管理任务。"

    def _list_jobs(self) -> str:
        """列出所有已调度任务。"""
        if self._cron is None:
            return "错误：Cron 服务未初始化"

        jobs = self._cron.list_jobs()
        if not jobs:
            return "没有已调度的任务。"
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  用途: {self._system_job_purpose(j)}")
                parts.append("  保护状态：可见但不可删除。")
            parts.extend(self._format_state(j))
            lines.append("\n".join(parts))
        return "已调度的任务:\n" + "\n".join(lines)

    def _remove_job(self, **kwargs: Any) -> str:
        """移除定时任务。"""
        if self._cron is None:
            return "错误：Cron 服务未初始化"

        job_id = kwargs.get("job_id", "")
        if not job_id:
            return "错误：remove 操作需要 job_id 参数"

        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"已移除任务 {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "无法移除任务 `dream`。\n"
                    "这是系统管理的 Dream 记忆合并任务，用于长期记忆。\n"
                    "它可见但不可删除。"
                )
            return f"无法移除任务 `{job_id}`。这是受保护的系统内部任务。"
        return f"任务 {job_id} 未找到"

    async def _on_job_execute(self, job: CronJob) -> str | None:
        """Cron 任务触发时的回调：通过 Agent Loop 执行任务消息。

        如果 job.payload.deliver 为 True，通过 event_bus 发布出站消息，
        由通道插件订阅后投递给用户，不经过 LLM 处理，避免递归。

        Args:
            job: 触发执行的任务

        Returns:
            Agent 的回复文本
        """
        if not self.kernel or not self.kernel.agent_loop:
            logger.warning("Cron: Agent Loop 不可用，无法执行任务 {}", job.id)
            return None

        if not job.payload.message:
            logger.warning("Cron: 任务 {} 消息为空，跳过执行", job.id)
            return None

        logger.info("Cron: 触发任务 {}, 消息: {}", job.id, job.payload.message[:100])
        try:
            # deliver=True 的任务：直接投递给用户，不经过 LLM 处理，避免递归
            if job.payload.deliver:
                content_text = job.payload.message
                logger.info("Cron: 任务 {} 交付用户（跳过 LLM）", job.id)

                channel = job.payload.channel or "cli"
                chat_id = job.payload.to or "direct"
                # 剥离 chat_id 中的通道前缀（如 "dingtalk:shenqla" → "shenqla"），
                # 避免 DingTalk API 因 userId 格式不正确而报 staffId.notExisted
                if ":" in chat_id:
                    chat_id = chat_id.split(":", 1)[-1]

                if self.kernel.agent_loop.event_bus:
                    await self.kernel.agent_loop.event_bus.publish("agent.outbound", {
                        "channel": channel,
                        "chat_id": chat_id,
                        "content": content_text,
                        "metadata": job.payload.channel_meta or {},
                    })
                return content_text

            # deliver=False 的任务：交给 LLM 处理（作为 agent 内部指令）
            context_id = job.payload.user_id or job.payload.to or "cron"
            result = await self.kernel.handle_message(
                message=job.payload.message,
                context_id=context_id,
                sender_id="system",
            )
            content_text = result.content if result else ""

            return content_text
        except Exception:
            logger.exception("Cron: 任务 {} 执行异常", job.id)
            return None
