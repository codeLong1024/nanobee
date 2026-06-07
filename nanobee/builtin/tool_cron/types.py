"""Cron 类型定义 — 定时任务数据类。

从 nanobot/cron/types.py 移植。
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
    deliver: bool = False
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

    @classmethod
    def from_dict(cls, kwargs: dict) -> CronJob:
        """从字典反序列化（兼容 JSON 持久化格式）。"""
        state_kwargs = dict(kwargs.get("state", {}))
        state_kwargs["run_history"] = [
            r if isinstance(r, CronRunRecord) else CronRunRecord(**r)
            for r in state_kwargs.get("run_history", [])
        ]
        kwargs["schedule"] = CronSchedule(**kwargs.get("schedule", {"kind": "every"}))
        kwargs["payload"] = CronPayload(**kwargs.get("payload", {}))
        kwargs["state"] = CronJobState(**state_kwargs)
        return cls(**kwargs)


@dataclass
class CronStore:
    """持久化存储。"""

    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
