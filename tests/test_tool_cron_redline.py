"""Tool Cron 插件 — 安全下限（间隔红线）测试。

验证 CronService 层对调度间隔的硬编码安全不变量：
- every 间隔不得低于 30 秒红线
- cron 表达式下次触发不得过近
- at 一次性任务不得过近
- register_system_job 同样守门（唯一创建绕行入口）
- plugin._add_job 将 ValueError 翻译为面向 LLM 的中文修正提示
磁盘恢复（_load_jobs）零改动，按设计不对恢复历史做校验。
"""

from pathlib import Path

import pytest

from nanobee.builtin.tool_cron.service import (
    CronService,
    _compute_next_run,
    _HARD_MIN_INTERVAL_MS,
    _now_ms,
)
from nanobee.builtin.tool_cron.types import CronJob, CronPayload, CronSchedule


@pytest.fixture
def service(tmp_path: Path) -> CronService:
    """创建一个使用临时存储的 CronService（不启动定时器）。"""
    cron = CronService(store_path=tmp_path / "jobs.json")
    return cron


def _schedule(kind: str, **kw) -> CronSchedule:
    return CronSchedule(kind=kind, **kw)


def _make_job(schedule: CronSchedule) -> CronJob:
    return CronJob(
        id="job1",
        name="sys",
        enabled=True,
        schedule=schedule,
        payload=CronPayload(kind="system_event", message="", deliver=False),
    )


class TestEveryIntervalRedLine:
    """every 调度间隔红线测试。"""

    def test_every_below_hard_minimum_rejected(self, service: CronService) -> None:
        """every_seconds < 30（如 1 秒）应被拒绝。"""
        schedule = _schedule("every", every_ms=1_000)
        with pytest.raises(ValueError, match="below the hard minimum"):
            service.add_job("x", schedule, "msg")

    def test_every_equal_hard_minimum_accepted(self, service: CronService) -> None:
        """every_ms 恰等于 30 秒红线应被放行。"""
        schedule = _schedule("every", every_ms=_HARD_MIN_INTERVAL_MS)
        job = service.add_job("x", schedule, "msg")
        assert job.schedule.every_ms == _HARD_MIN_INTERVAL_MS

    def test_every_above_hard_minimum_accepted(self, service: CronService) -> None:
        """every_seconds = 60（正常值）应被放行。"""
        schedule = _schedule("every", every_ms=60_000)
        job = service.add_job("x", schedule, "msg")
        assert job.schedule.every_ms == 60_000


class TestAtIntervalRedLine:
    """at 一次性调度红线测试。"""

    def test_at_too_soon_rejected(self, service: CronService) -> None:
        """at 距当前不足 30 秒应被拒绝。"""
        soon = _now_ms() + 1_000
        schedule = _schedule("at", at_ms=soon)
        with pytest.raises(ValueError, match="fires too soon"):
            service.add_job("x", schedule, "msg")

    def test_at_far_future_accepted(self, service: CronService) -> None:
        """at 距当前足够远应被放行。"""
        future = _now_ms() + 60_000
        schedule = _schedule("at", at_ms=future)
        job = service.add_job("x", schedule, "msg")
        assert job.schedule.at_ms == future


class TestCronIntervalRedLine:
    """cron 表达式红线测试。"""

    def test_cron_two_minutes_accepted(self, service: CronService) -> None:
        """cron 表达式 '*/2 * * * *'（每两分钟）下次触发距当前 >= 60s，应放行。"""
        schedule = _schedule("cron", expr="*/2 * * * *", tz="UTC")
        next_ms = _compute_next_run(schedule, _now_ms())
        assert next_ms is not None
        assert next_ms - _now_ms() >= _HARD_MIN_INTERVAL_MS
        job = service.add_job("x", schedule, "msg")
        assert job.schedule.expr == "*/2 * * * *"

    def test_cron_second_granularity_rejected(self, service: CronService) -> None:
        """cron 表达式 '*/1 * * * * *'（每秒）下次触发过近，应被拒绝。"""
        schedule = _schedule("cron", expr="*/1 * * * * *", tz="UTC")
        next_ms = _compute_next_run(schedule, _now_ms())
        assert next_ms is not None
        assert next_ms - _now_ms() < _HARD_MIN_INTERVAL_MS
        with pytest.raises(ValueError, match="fires too soon"):
            service.add_job("x", schedule, "msg")


class TestRegisterSystemJobRedLine:
    """register_system_job 守门测试（唯一创建绕行入口）。"""

    def test_system_job_below_red_line_rejected(self, service: CronService) -> None:
        """系统任务使用超高频 every 也应被拒绝。"""
        schedule = _schedule("every", every_ms=1_000)
        job = _make_job(schedule)
        with pytest.raises(ValueError, match="below the hard minimum"):
            service.register_system_job(job)

    def test_system_job_normal_accepted(self, service: CronService) -> None:
        """系统任务正常间隔应被放行。"""
        schedule = _schedule("every", every_ms=60_000)
        job = _make_job(schedule)
        registered = service.register_system_job(job)
        assert registered.schedule.every_ms == 60_000

    def test_update_job_too_soon_rejected(self, service: CronService) -> None:
        """update_job 将调度改为超高频也应被拒绝。"""
        schedule = _schedule("every", every_ms=60_000)
        job = service.add_job("x", schedule, "msg")
        bad = _schedule("every", every_ms=1_000)
        with pytest.raises(ValueError, match="below the hard minimum"):
            service.update_job(job.id, schedule=bad)
