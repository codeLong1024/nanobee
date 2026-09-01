"""Cron 类型定义 — 定时任务数据类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CronSchedule:
    """调度定义。"""

    kind: Literal["at", "every", "cron"]
    # 一次性任务：时间戳（毫秒）
    at_ms: int | None = None
    # 间隔任务：毫秒
    every_ms: int | None = None
    # cron 表达式（如 "0 9 * * *"）
    expr: str | None = None
    # 时区（仅用于 cron 表达式）
    tz: str | None = None


@dataclass
class CronPayload:
    """任务触发时的执行负载。"""

    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = True
    channel: str | None = None
    to: str | None = None
    channel_meta: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None
    user_id: str | None = None


@dataclass
class CronRunRecord:
    """单次执行记录。"""

    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None


@dataclass
class CronJobState:
    """任务运行时状态。"""

    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)


@dataclass
class CronJob:
    """一个完整的定时任务。"""

    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """持久化存储。"""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)


class CronJobError(Exception):
    """Cron 任务执行失败。

    由插件层在识别到执行错误（agent 内部错误通知或调用异常）后抛出，
    CronService._execute_job 捕获后记录 last_status="error" 与 last_error，
    使 cron list 能如实反映失败状态。
    """
