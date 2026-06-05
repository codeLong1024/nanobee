"""Tool Cron 插件 — 定时任务工具（add, list, remove）。

基于 nanobot/agent/tools/cron.py 适配 nanobee 插件架构。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nanobee.builtin.tool_cron.service import CronService
from nanobee.builtin.tool_cron.types import CronJob, CronSchedule
from nanobee.plugins.tool import ToolPlugin

logger = logging.getLogger(__name__)


class ToolCronPlugin(ToolPlugin):
    """Cron 定时任务工具插件。

    提供 cron 工具的三个操作（add / list / remove）。
    通过 set_context() 注入当前会话的通道信息，用于任务触发后投递结果。
    """

    name = "tool-cron"
    version = "1.0.0"
    plugin_type = "tool"

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)
        self._cron: CronService | None = None
        self._default_timezone: str = "UTC"
        # 会话上下文（从 set_context 注入）
        self._channel: str = ""
        self._chat_id: str = ""
        self._user_id: str = ""
        self._context_metadata: dict[str, Any] = {}
        self._session_key: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def initialize(self, kernel: Any) -> None:
        """初始化插件：创建 CronService 实例。"""
        super().initialize(kernel)

        self._default_timezone = self.get_config("default_timezone", "UTC")

        # 构建存储路径：~/.nanobee/cron/（按用户隔离）
        work_dir = Path(kernel.work_dir) if hasattr(kernel, "work_dir") else Path.cwd()
        self._cron_base_dir = work_dir / "cron"
        self._cron_base_dir.mkdir(parents=True, exist_ok=True)
        self._current_store_path: Path | None = None

        logger.info("Cron 服务基础目录: %s", self._cron_base_dir)

    def on_enable(self) -> None:
        """启用时启动 CronService。"""
        super().on_enable()
        if self._cron is not None:
            try:
                self._cron.start()
                self._enabled = True
            except RuntimeError as e:
                logger.error("Cron 服务启动失败: %s", e)

    def on_disable(self) -> None:
        """禁用时停止 CronService。"""
        if self._cron is not None:
            self._cron.stop()
        super().on_disable()

    # ------------------------------------------------------------------
    # 上下文注入（由外部 runner 在每次 execute_tool 前调用）
    # ------------------------------------------------------------------

    def set_context(
        self,
        channel: str = "",
        chat_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
        session_key: str = "",
    ) -> None:
        """设置当前会话上下文（在 execute_tool 前调用）。

        Args:
            channel: 消息通道标识
            chat_id: 会话/聊天 ID
            user_id: 用户唯一标识（用于按用户隔离存储 cron jobs）
            metadata: 通道特定元数据
            session_key: 会话键（用于 session 记录）
        """
        self._channel = channel
        self._chat_id = chat_id
        self._user_id = user_id
        self._context_metadata = metadata or {}
        self._session_key = session_key

        # 根据 user_id 切换存储路径
        if user_id:
            self._current_store_path = self._cron_base_dir / f"jobs_{user_id}.json"
            if self._cron is None:
                self._cron = CronService(
                    store_path=self._current_store_path,
                    on_job=self._on_job_execute,
                    max_sleep_ms=300_000,
                )
                logger.info("Cron 服务存储路径: %s", self._current_store_path)
        else:
            self._current_store_path = None

    # ------------------------------------------------------------------
    # ToolPlugin 接口
    # ------------------------------------------------------------------

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
                        "调度提醒和重复性任务。操作：add（添加）、list（列出）、remove（移除）。"
                        " add 需要 message 参数加一种调度方式（every_seconds / cron_expr / at）；"
                        " remove 需要 job_id；list 仅需 action。"
                        f" 省略 tz 时 cron 表达式和裸 ISO 时间默认使用 {self._default_timezone}。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "list", "remove"],
                                "description": "要执行的操作。add=添加任务, list=列出所有任务, remove=移除任务",
                            },
                            "name": {
                                "type": "string",
                                "description": "可选的任务短标签（如 'weather-monitor'），默认取 message 前 30 字符",
                            },
                            "message": {
                                "type": "string",
                                "description": "action='add' 时必填。任务触发时 agent 执行的具体指令",
                            },
                            "every_seconds": {
                                "type": "integer",
                                "description": "重复间隔（秒），用于周期性任务",
                                "minimum": 1,
                            },
                            "cron_expr": {
                                "type": "string",
                                "description": "Cron 表达式，如 '0 9 * * *'（每天 9:00）",
                            },
                            "tz": {
                                "type": "string",
                                "description": "可选 IANA 时区（仅用于 cron_expr），如 'Asia/Shanghai'",
                            },
                            "at": {
                                "type": "string",
                                "description": "一次性执行 ISO 时间，如 '2026-02-12T10:30:00'",
                            },
                            "deliver": {
                                "type": "boolean",
                                "description": "是否将执行结果投递到用户通道（默认 true）",
                            },
                            "job_id": {
                                "type": "string",
                                "description": "action='remove' 时必填。通过 action='list' 获取的任务 ID",
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

        action = kwargs.get("action", "").strip().lower()
        if action == "add":
            return self._add_job(**kwargs)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(**kwargs)
        return f"未知操作: {action}"

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 操作实现
    # ------------------------------------------------------------------

    def _add_job(self, **kwargs: Any) -> str:
        """添加定时任务。"""
        if self._cron is None:
            return "错误：Cron 服务未初始化"

        message = kwargs.get("message", "")
        if not message:
            return (
                "错误：cron action='add' 需要非空 message 参数描述触发时执行的操作。"
                "请重试并包含 message=\"...\"。"
            )

        channel = self._channel
        chat_id = self._chat_id
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
            channel_meta=self._context_metadata,
            session_key=self._session_key or None,
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

    # ------------------------------------------------------------------
    # 任务触发回调
    # ------------------------------------------------------------------

    async def _on_job_execute(self, job: CronJob) -> str | None:
        """Cron 任务触发时的回调：通过 Agent Loop 执行任务消息。

        Args:
            job: 触发执行的任务

        Returns:
            Agent 的回复文本
        """
        if not self.kernel or not self.kernel.agent_loop:
            logger.warning("Cron: Agent Loop 不可用，无法执行任务 %s", job.id)
            return None

        if not job.payload.message:
            logger.warning("Cron: 任务 %s 消息为空，跳过执行", job.id)
            return None

        logger.info("Cron: 触发任务 %s, 消息: %s", job.id, job.payload.message[:100])
        try:
            result = await self.kernel.handle_message(
                message=job.payload.message,
                context_id=job.payload.to or "cron",
                sender_id="system",
            )
            content_text = result.content if result else ""
            if job.payload.deliver and content_text:
                logger.info("Cron: 任务 %s 执行完成", job.id)
            return content_text
        except Exception:
            logger.exception("Cron: 任务 %s 执行异常", job.id)
            return None
