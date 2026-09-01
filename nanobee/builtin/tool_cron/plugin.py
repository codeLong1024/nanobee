"""Tool Cron 插件 — 定时任务工具（add, list, remove）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from nanobee.builtin.tool_cron.service import CronService
from nanobee.builtin.tool_cron.types import CronJob, CronJobError, CronSchedule
from nanobee.kernel.context_sandbox_var import current_request_context
from nanobee.plugins import ToolPlugin

from nanobee.utils.logger import logger

# 框架系统通知契约（与 notifications.build_notification 写入的 metadata 对齐；
# 框架侧契约变更时需同步此处，否则错误会被静默误判为成功）。
_SYSTEM_NOTIFY_TYPE = "system"
_SYSTEM_NOTIFY_SEVERITY = "error"


class ToolCronPlugin(ToolPlugin):
    """Cron 定时任务工具插件。

    提供 cron 工具的三个操作（add / list / remove）。
    通过 CURRENT_REQUEST_CONTEXT ContextVar 按 turn 获取会话信息，
    按 context_id 隔离 CronService 实例，多用户并发互不干扰。
    """

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)
        self._crons: dict[str, CronService] = {}
        self._default_timezone: str = "UTC"

    def initialize(self, kernel: Any) -> None:
        """初始化插件：创建 CronService 实例。"""
        super().initialize(kernel)

        self._default_timezone = self.get_config("default_timezone", "UTC")

        logger.info("Cron 插件初始化完成")

    def on_enable(self) -> None:
        """启用时扫描并启动所有用户已有的 CronService。"""
        super().on_enable()
        self._scan_existing_jobs()

    def _scan_existing_jobs(self) -> None:
        """扫描 users 目录下所有用户的 cron 任务文件，为每个用户创建 CronService。"""
        if not self.kernel:
            return
        data_dir = Path(self.kernel.data_dir).expanduser()
        users_base = data_dir / "users"
        if not users_base.is_dir():
            return
        for user_dir in sorted(users_base.iterdir()):
            context_id = user_dir.name
            cron_file = user_dir / "cron" / "jobs.json"
            if cron_file.is_file():
                cron = CronService(
                    store_path=cron_file,
                    on_job=self._on_job_execute,
                    max_sleep_ms=300_000,
                )
                try:
                    cron.start()
                except RuntimeError as e:
                    logger.error("Cron: 恢复用户 {} 的任务失败: {}", context_id, e)
                    continue
                self._crons[context_id] = cron
                logger.info("Cron: 已恢复用户 {} 的任务", context_id)

    def on_disable(self) -> None:
        """禁用时停止所有 CronService。"""
        for cron in self._crons.values():
            cron.stop()
        self._crons.clear()
        super().on_disable()

    def _resolve_store_path(self, context_id: str) -> Path:
        """根据 context_id 解析 cron 任务存储路径。

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
            data_dir = Path(self._kernel.data_dir) if self._kernel and hasattr(self._kernel, "data_dir") else Path.cwd()
            base_dir = data_dir / "cron"
            base_dir.mkdir(parents=True, exist_ok=True)
            store_path = base_dir / f"jobs_{context_id}.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return store_path

    def _ensure_cron_service(self, context_id: str, store_path: Path) -> CronService:
        """确保指定 context_id 对应的 CronService 已初始化。

        Args:
            context_id: 用户上下文 ID
            store_path: cron 任务存储路径

        Returns:
            对应的 CronService 实例
        """
        if context_id not in self._crons:
            cron = CronService(
                store_path=store_path,
                on_job=self._on_job_execute,
                max_sleep_ms=300_000,
            )
            self._crons[context_id] = cron
            logger.info("Cron 服务存储路径: {}", store_path)
        return self._crons[context_id]

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
                                "description": "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers (e.g., 'Check system status and report' or 'Send a daily standup summary'). The agent will execute this via LLM and reply to the user. Not used for action='list' or action='remove'.",
                            },
                            "every_seconds": {
                                "type": "integer",
                                "description": "Interval in seconds for recurring tasks. Minimum: 30.",
                                "minimum": 30,
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
        cron = self._ensure_cron_service(rctx.context_id, store_path)

        action = kwargs.get("action", "").strip().lower()
        if action == "add":
            return self._add_job(
                cron=cron,
                channel=rctx.channel,
                chat_id=rctx.chat_id,
                context_metadata={},
                session_key=rctx.session_id,
                user_id=rctx.context_id,
                **kwargs,
            )
        elif action == "list":
            return self._list_jobs(cron)
        elif action == "remove":
            return self._remove_job(cron=cron, **kwargs)
        return f"未知操作: {action}"

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        """校验 IANA 时区，无效时返回错误信息。"""
        try:
            ZoneInfo(tz)
        except KeyError:
            return f"错误：未知时区 '{tz}'"
        return None

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        """格式化时间戳为可读字符串。"""
        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    def _add_job(
        self,
        *,
        cron: CronService,
        channel: str = "",
        chat_id: str = "",
        context_metadata: dict[str, Any] | None = None,
        session_key: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> str:
        """添加定时任务。

        Args:
            cron: 当前用户的 CronService 实例
            channel: 来源通道名
            chat_id: 会话/聊天 ID
            context_metadata: 通道附加元数据
            session_key: 会话键
            user_id: 用户唯一标识
        """
        # 确保服务已启动（懒加载模式：在首次添加任务时启动）
        if not cron.is_running:
            try:
                cron.start()
                logger.info("Cron 服务已启动（懒加载）")
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

        try:
            job = cron.add_job(
                name=name,
                schedule=schedule,
                message=message,
                channel=channel,
                to=chat_id,
                delete_after_run=delete_after,
                channel_meta=context_metadata or {},
                session_key=session_key or None,
                user_id=user_id or None,
            )
        except ValueError as e:
            # ValueError 只可能来自安全不变量校验（如间隔低于硬编码红线）
            return (
                f"错误：{e}\n"
                "这是系统的安全下限保护。请将 every_seconds 调大（建议至少 60 秒），"
                "或把 cron_expr / at 改到更远的未来后重试。"
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

    def _list_jobs(self, cron: CronService) -> str:
        """列出所有已调度任务。

        Args:
            cron: 当前用户的 CronService 实例
        """
        jobs = cron.list_jobs()
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

    def _remove_job(self, *, cron: CronService, **kwargs: Any) -> str:
        """移除定时任务。

        Args:
            cron: 当前用户的 CronService 实例
        """
        job_id = kwargs.get("job_id", "")
        if not job_id:
            return "错误：remove 操作需要 job_id 参数"

        result = cron.remove_job(job_id)
        if result == "removed":
            return f"已移除任务 {job_id}"
        if result == "protected":
            job = cron.get_job(job_id)
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

        所有 cron 任务统一通过 handle_message 交给 LLM 处理（可调用 skill），
        并把执行结果投递回创建任务的原始会话，让用户收到 LLM 的回复。

        错误透传：两类失败均会向用户投递带任务标识（name + id）的错误通知，
        并抛出 CronJobError 让 service 层记录 last_status="error"：
        1. agent 层内部错误：handle_message 返回 turn_internal_error 系统通知
           （metadata 携带 notification_type=system + severity=error）
        2. 调用异常：handle_message 本身抛出异常
        空结果（返回 None / 空 content）视为静默成功，不投递不报错。

        Args:
            job: 触发执行的任务

        Returns:
            Agent 的回复文本（失败场景抛异常而非返回）

        Raises:
            CronJobError: agent 层识别到执行失败，或执行成功但结果投递失败
            Exception: handle_message 的原始异常（投递通知后原样上抛）
        """
        if not self.kernel or not self.kernel.agent_loop:
            logger.warning("Cron: Agent Loop 不可用，无法执行任务 {}", job.id)
            return None

        if not job.payload.message:
            logger.warning("Cron: 任务 {} 消息为空，跳过执行", job.id)
            return None

        logger.info("Cron: 触发任务 {}, 消息: {}", job.id, job.payload.message[:100])
        try:
            # 所有 cron 任务统一交给 LLM 处理（作为 agent 内部指令，可调用 skill），
            # 并把执行结果投递回创建任务的原始会话，让用户收到 LLM 的回复。
            channel, chat_id = self._delivery_target(job) or ("cli", "direct")
            # 用户隔离键 = 创建任务的用户（job.payload.user_id，创建时来自 rctx.context_id）。
            # 缺失时（系统级任务，如 dream）兜底为 "system"，避免所有 cron 共享一个隔离目录。
            sender_id = job.payload.user_id or "system"
            result = await self.kernel.handle_message(
                message=job.payload.message,
                context_id=chat_id,
                channel=channel,
                sender_id=sender_id,
                # cron 定时触发是无上下文场景，使用 fresh_session 隔离空会话，
                # 避免拉取该用户历史对话（token 浪费 + 上下文污染），turn 结束后自动回收。
                fresh_session=True,
            )
        except Exception as exc:
            # 调用异常：投递带任务标识的错误通知（_deliver 自吞投递失败并留栈，不遮蔽原始异常），
            # 再原样上抛由 service._execute_job 记录 last_error。
            logger.exception("Cron: 任务 {} 执行异常", job.id)
            await self._deliver(
                job,
                self._build_error_notice(job, f"{type(exc).__name__}: {exc}"),
                severity="error",
            )
            raise

        meta = getattr(result, "metadata", {}) or {}
        if meta.get("notification_type") == _SYSTEM_NOTIFY_TYPE and meta.get("severity") == _SYSTEM_NOTIFY_SEVERITY:
            # agent 层已把错误折叠为 turn_internal_error 系统通知（不抛异常），
            # 补上任务标识重新投递，并抛出让 service 记录失败状态（修复 cron list 误报 ok）。
            detail = str(meta.get("error_detail") or getattr(result, "content", "") or "未知错误")
            # 投递失败不改变失败判定：_deliver 自吞失败并留栈，CronJobError 必然执行
            await self._deliver(job, self._build_error_notice(job, detail), severity="error")
            raise CronJobError(detail)

        content_text = result.content if result else ""

        # 投递失败 = 用户未收到结果，显式记"结果投递失败"（与"执行失败"可区分，cron list 可定位）
        if not await self._deliver(job, content_text):
            raise CronJobError(f"任务已执行但结果投递失败（内容长度 {len(content_text)}）")

        return content_text

    @staticmethod
    def _build_error_notice(job: CronJob, detail: str) -> str:
        """构造含任务标识的错误通知文案。

        Args:
            job: 执行失败的任务
            detail: 错误详情（透传真实异常信息，不编造）

        Returns:
            含任务名与任务 ID 的错误通知文本
        """
        return f"定时任务「{job.name}」({job.id}) 执行失败：\n{detail}"

    @staticmethod
    def _delivery_target(job: CronJob) -> tuple[str, str] | None:
        """解析任务的投递目标 ``(channel, chat_id)``。

        任务必须带有效投递目标（channel 与 to 均非空）才可投递；
        否则返回 None，表示不向任何通道投递出站消息。

        Args:
            job: 触发执行的任务

        Returns:
            ``(channel, chat_id)`` 元组；无有效投递目标时返回 None
        """
        if not job.payload.channel or not job.payload.to:
            return None
        chat_id = job.payload.to
        # 剥离通道前缀（如 "dingtalk:<userid>" → 纯 userid，避免 API 格式错误）
        if ":" in chat_id:
            chat_id = chat_id.split(":", 1)[-1]
        return job.payload.channel, chat_id

    async def _deliver(
        self, job: CronJob, content: str, severity: Literal["info", "error"] = "info"
    ) -> bool:
        """通过 agent.outbound 事件投递内容到任务的原会话。

        复用 agent.outbound 事件机制，由通道插件订阅后投递给用户。
        内容为空或无有效投递目标时不投递，视为跳过（返回 True，不误报失败）。
        severity="error" 时 metadata 携带系统通知标记（notification_type/severity），
        通道可据此差异化渲染（复用 subagent_spawned 已验证的投递路径）。
        投递失败由本方法自行记录堆栈并返回 False，调用方据此区分
        "执行失败" 与 "执行成功但投递失败"。

        Args:
            job: 触发执行的任务
            content: 要投递的消息内容
            severity: 消息严重程度（info / error），默认 info

        Returns:
            是否投递成功（跳过视为成功）
        """
        # 守卫与发布必须使用同一 event_bus 引用，避免"守卫通过但发布目标不可用"的不对称
        loop = self.kernel.agent_loop if self.kernel else None
        if not content or loop is None or loop.event_bus is None:
            return True
        target = self._delivery_target(job)
        if target is None:
            return True
        channel, chat_id = target
        metadata = dict(job.payload.channel_meta or {})
        if severity == "error":
            metadata.update({
                "notification_type": _SYSTEM_NOTIFY_TYPE,
                "notification_kind": "cron_job_error",
                "severity": _SYSTEM_NOTIFY_SEVERITY,
            })
        try:
            await loop.event_bus.publish("agent.outbound", {
                "channel": channel,
                "chat_id": chat_id,
                "content": content,
                "metadata": metadata,
            })
        except Exception:
            # 投递失败自行留栈（含 job 标识与目标通道），供调用方与 cron list 定位根因
            logger.exception("Cron: 任务 {} 结果投递失败（channel={}）", job.id, channel)
            return False
        return True
