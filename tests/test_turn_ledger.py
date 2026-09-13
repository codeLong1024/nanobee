"""TurnLedger / TurnReport 数据结构单元测试。

验证账本纪律的最小不变量：
1. 纯数据类：默认构造可用、slots 生效、无行为方法；
2. AgentRunResult.ledger 为增量字段且默认实例不共享（default_factory 隔离）；
3. TurnReport 承载 loop 盖章字段（turn_id / turn_started_at）与窗口引用。
"""

from dataclasses import fields

import pytest

from nanobee.agent.specs import (
    AgentRunResult,
    InjectionFact,
    IterationFact,
    TurnLedger,
    TurnReport,
)


class TestIterationFact:
    """IterationFact 结构测试。"""

    def test_default_construction(self) -> None:
        """no 为唯一必填字段，其余带默认值。"""
        fact = IterationFact(no=0)
        assert fact.no == 0
        assert fact.llm_call_ms == 0.0
        assert fact.finish_reason == ""
        assert fact.usage == {}
        assert fact.tool_call_ids == []

    def test_full_construction(self) -> None:
        """全字段构造保持原值。"""
        fact = IterationFact(
            no=2,
            llm_call_ms=123.5,
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            tool_call_ids=["call_1"],
        )
        assert fact.no == 2
        assert fact.llm_call_ms == 123.5
        assert fact.finish_reason == "tool_calls"
        assert fact.usage == {"prompt_tokens": 10, "completion_tokens": 5}
        assert fact.tool_call_ids == ["call_1"]

    def test_is_slots_dataclass(self) -> None:
        """slots 生效：不允许动态属性。"""
        fact = IterationFact(no=0)
        with pytest.raises(AttributeError):
            fact.arbitrary = 1  # type: ignore[attr-defined]


class TestInjectionFact:
    """InjectionFact 结构测试。"""

    def test_default_construction(self) -> None:
        """count 为唯一必填字段。"""
        fact = InjectionFact(count=2)
        assert fact.count == 2
        assert fact.phase == ""

    def test_full_construction(self) -> None:
        """phase 保留 drain 阶段标识。"""
        fact = InjectionFact(count=3, phase="after tool execution")
        assert fact.count == 3
        assert fact.phase == "after tool execution"


class TestTurnLedger:
    """TurnLedger 结构测试。"""

    def test_default_construction(self) -> None:
        """全字段默认值。"""
        ledger = TurnLedger()
        assert ledger.turn_input_index == 0
        assert ledger.iterations == []
        assert ledger.injections == []
        assert ledger.exit_reason == ""
        assert ledger.error is None

    def test_ledger_instances_isolated(self) -> None:
        """默认可变字段不共享（default_factory 隔离）。"""
        a = TurnLedger()
        b = TurnLedger()
        a.iterations.append(IterationFact(no=0))
        a.injections.append(InjectionFact(count=1))
        assert b.iterations == []
        assert b.injections == []

    def test_declared_fields(self) -> None:
        """字段集合固定，防止无评审的契约漂移。"""
        names = {f.name for f in fields(TurnLedger)}
        assert names == {
            "turn_input_index",
            "iterations",
            "injections",
            "exit_reason",
            "error",
        }


class TestTurnReport:
    """TurnReport 结构测试。"""

    def test_minimal_construction(self) -> None:
        """必填字段 + 盖章外字段默认空。"""
        ledger = TurnLedger()
        report = TurnReport(
            turn_id="a" * 32,
            turn_started_at="2026-09-13T10:00:00+08:00",
            ledger=ledger,
        )
        assert report.turn_id == "a" * 32
        assert report.turn_started_at == "2026-09-13T10:00:00+08:00"
        assert report.ledger is ledger
        assert report.turn_ended_at == ""
        assert report.messages_window == []

    def test_loop_stamp_fields(self) -> None:
        """loop 盖章三值（turn_id / started_at / ended_at）均可承载。"""
        report = TurnReport(
            turn_id="a" * 32,
            turn_started_at="2026-09-13T10:00:00+08:00",
            ledger=TurnLedger(),
            turn_ended_at="2026-09-13T10:00:03+08:00",
            messages_window=[{"role": "user", "content": "hi"}],
        )
        assert report.turn_ended_at == "2026-09-13T10:00:03+08:00"
        assert report.messages_window == [{"role": "user", "content": "hi"}]

    def test_ledger_is_reference_not_copy(self) -> None:
        """ledger 为引用语义：runner 后续追加对 report 可见。"""
        ledger = TurnLedger()
        report = TurnReport(turn_id="t", turn_started_at="x", ledger=ledger)
        ledger.iterations.append(IterationFact(no=0))
        assert len(report.ledger.iterations) == 1


class TestAgentRunResultLedger:
    """AgentRunResult.ledger 增量字段测试。"""

    def _make_result(self) -> AgentRunResult:
        return AgentRunResult(final_content="ok", messages=[])

    def test_default_ledger(self) -> None:
        """默认空账本，现有构造点零破坏。"""
        result = self._make_result()
        assert isinstance(result.ledger, TurnLedger)
        assert result.ledger.iterations == []

    def test_ledger_not_shared_between_results(self) -> None:
        """两次构造的 ledger 不共享（default_factory 隔离）。"""
        a = self._make_result()
        b = self._make_result()
        a.ledger.iterations.append(IterationFact(no=0))
        assert b.ledger.iterations == []

    def test_explicit_ledger_preserved(self) -> None:
        """显式传入的账本原样保留。"""
        ledger = TurnLedger(exit_reason="completed", error=None)
        result = AgentRunResult(final_content="ok", messages=[], ledger=ledger)
        assert result.ledger is ledger
