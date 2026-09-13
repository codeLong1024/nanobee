"""Tool Cron 插件 — 安全下限（间隔红线）测试。

验证 CronService 层对调度间隔的硬编码安全不变量：
- every 间隔不得低于 30 秒红线
- cron 表达式相邻两次触发间隔不得低于 30 秒红线（与墙钟相位无关）
- at 一次性任务不得过近
- register_system_job 同样守门（唯一创建绕行入口）
- plugin._add_job 将 ValueError 翻译为面向 LLM 的中文修正提示
磁盘恢复（_load_jobs）零改动，按设计不对恢复历史做校验。

cron 用例一律冻结时钟：红线判据必须与"插入时刻"解耦，否则用例本身会随墙钟相位
间歇性失败（历史事故与判据修正见 devdocs/tool_cron_间隔红线_判据修正方案_20260913.md）。
"""

import datetime as dt
from pathlib import Path

import pytest

from nanobee.builtin.tool_cron import service as service_module
from nanobee.builtin.tool_cron.service import (
    _HARD_MIN_INTERVAL_MS,
    CronService,
    _compute_min_interval_ms,
    _now_ms,
)
from nanobee.builtin.tool_cron.types import CronJob, CronPayload, CronSchedule


@pytest.fixture
def service(tmp_path: Path) -> CronService:
    """创建一个使用临时存储的 CronService（不启动定时器）。"""
    cron = CronService(store_path=tmp_path / "jobs.json")
    return cron


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, wall: dt.datetime) -> None:
    """把 CronService 的时间源冻结到指定时刻（消除墙钟相位依赖）。"""
    fixed_ms = int(wall.timestamp() * 1000)
    monkeypatch.setattr(service_module, "_now_ms", lambda: fixed_ms)


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
    """cron 表达式红线测试：判据为相邻触发间隔，与墙钟相位无关。

    历史事故：旧判据是"首触发距现在 >= 30s"（相位属性），导致两个方向的错误——
    ① 误伤周期合法的表达式（`*/2 * * * *` 在周期末段被拒，25% 墙钟相位必挂）；
    ② 漏放亚 30s 间隔的秒级表达式（`* * * * * 0,45` 最小间隔 15s，75% 相位被放行）。
    下列用例对两个方向分别做多相位扫描回归。
    """

    # 基准墙钟：偶数分钟整点（对 `*/2` 这类表达式即周期起点）
    _BASE = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.UTC)

    def test_cron_two_minutes_accepted_at_any_phase(
        self, service: CronService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'*/2 * * * *'（周期 120 秒，合法）在任何相位都必须被接受。"""
        # 覆盖周期内各方位相位，重点是旧实现误伤的最后 30 秒（第 90~120 秒）
        for offset in (0, 7, 30, 45, 60, 89, 91, 105, 119):
            _freeze_clock(monkeypatch, self._BASE + dt.timedelta(seconds=offset))
            schedule = _schedule("cron", expr="*/2 * * * *", tz="UTC")
            job = service.add_job("x", schedule, "msg")
            assert job.schedule.expr == "*/2 * * * *"

    def test_cron_daily_far_future_accepted(
        self, service: CronService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'0 9 * * *'（每天 9 点）等首触发可能很远的表达式必须被接受。"""
        for offset in (0, 3_599, 43_199):
            _freeze_clock(monkeypatch, self._BASE + dt.timedelta(seconds=offset))
            schedule = _schedule("cron", expr="0 9 * * *", tz="UTC")
            job = service.add_job("x", schedule, "msg")
            assert job.schedule.expr == "0 9 * * *"

    def test_cron_sub_minute_interval_rejected_at_any_phase(
        self, service: CronService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'* * * * * 0,45'（每分第 0/45 秒，最小间隔 15 秒）在任何相位都必须被拒绝。

        回归旧实现的漏防：该表达式首触发等待可达 45 秒（> 30s 红线），
        因此约 75% 的相位会被旧判据放行，而它真会刷屏。
        """
        for offset in (0, 1, 44, 45, 50, 59):
            _freeze_clock(monkeypatch, self._BASE + dt.timedelta(seconds=offset))
            schedule = _schedule("cron", expr="* * * * * 0,45", tz="UTC")
            with pytest.raises(ValueError, match="fires too frequently"):
                service.add_job("x", schedule, "msg")

    def test_cron_second_granularity_rejected(
        self, service: CronService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'*/1 * * * * *'（每秒，间隔 1 秒）必须被拒绝。"""
        _freeze_clock(monkeypatch, self._BASE)
        schedule = _schedule("cron", expr="*/1 * * * * *", tz="UTC")
        with pytest.raises(ValueError, match="fires too frequently"):
            service.add_job("x", schedule, "msg")

    def test_cron_six_field_seconds_field_is_last(self) -> None:
        """croniter 六字段默认秒在末位：'0,45 * * * * *' 实为"每分第 0/45 分且每秒触发"。

        该约定直接决定"最小相邻间隔"从哪个字段算出，故在此显式锁定，
        避免误把首字段当秒而写出与预期不符的用例。
        """
        schedule = _schedule("cron", expr="0,45 * * * * *", tz="UTC")
        assert _compute_min_interval_ms(schedule, _now_ms()) == 1_000


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
