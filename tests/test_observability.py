"""observability 模块单元测试：Trace/Span ID 生成与校验、ContextVar、OTLP 常量、traceparent。"""

import asyncio
import re
import time

import pytest

from nanobee.utils.observability import (
    ATTR_CHANNEL,
    ATTR_CHAT_ID,
    ATTR_SENDER_ID,
    ATTR_SESSION_ID,
    ATTR_TOOL_NAME,
    ATTR_TURN_ID,
    SpanKind,
    StatusCode,
    format_traceparent,
    generate_span_id,
    generate_trace_id,
    get_parent_span_id,
    get_trace_id,
    is_valid_span_id,
    is_valid_trace_id,
    now_unix_nano,
    parse_traceparent,
    reset_parent_span_id,
    reset_trace_id,
    set_parent_span_id,
    set_trace_id,
)

HEX32_RE = re.compile(r"[0-9a-f]{32}")
HEX16_RE = re.compile(r"[0-9a-f]{16}")


# ---------------------------------------------------------------------------
# generate_trace_id / generate_span_id
# ---------------------------------------------------------------------------


class TestGenerateTraceId:
    def test_length_and_charset(self) -> None:
        """严格 32 位小写十六进制。"""
        assert HEX32_RE.fullmatch(generate_trace_id())

    def test_never_all_zero(self) -> None:
        for _ in range(100):
            assert generate_trace_id() != "0" * 32

    def test_self_valid(self) -> None:
        assert is_valid_trace_id(generate_trace_id())

    def test_uniqueness(self) -> None:
        ids = {generate_trace_id() for _ in range(1000)}
        assert len(ids) == 1000


class TestGenerateSpanId:
    def test_length_and_charset(self) -> None:
        """严格 16 位小写十六进制。"""
        assert HEX16_RE.fullmatch(generate_span_id())

    def test_never_all_zero(self) -> None:
        for _ in range(100):
            assert generate_span_id() != "0" * 16

    def test_self_valid(self) -> None:
        assert is_valid_span_id(generate_span_id())

    def test_uniqueness(self) -> None:
        ids = {generate_span_id() for _ in range(1000)}
        assert len(ids) == 1000


# ---------------------------------------------------------------------------
# is_valid_trace_id / is_valid_span_id
# ---------------------------------------------------------------------------


class TestIsValidTraceId:
    def test_valid_lowercase(self) -> None:
        assert is_valid_trace_id("a" * 32)

    def test_valid_uppercase_accepted(self) -> None:
        """校验宽松接受大写（内部 lower 后匹配）。"""
        assert is_valid_trace_id("A" * 32)

    def test_reject_wrong_length(self) -> None:
        assert not is_valid_trace_id("a" * 31)
        assert not is_valid_trace_id("a" * 33)

    def test_reject_non_hex(self) -> None:
        assert not is_valid_trace_id("z" * 32)
        assert not is_valid_trace_id("g" + "a" * 31)

    def test_reject_all_zero(self) -> None:
        assert not is_valid_trace_id("0" * 32)

    def test_reject_non_string_and_empty(self) -> None:
        assert not is_valid_trace_id(None)
        assert not is_valid_trace_id(123)
        assert not is_valid_trace_id("")


class TestIsValidSpanId:
    def test_valid(self) -> None:
        assert is_valid_span_id("b" * 16)
        assert is_valid_span_id("B" * 16)

    def test_reject_wrong_length(self) -> None:
        assert not is_valid_span_id("b" * 15)
        assert not is_valid_span_id("b" * 17)

    def test_reject_all_zero(self) -> None:
        assert not is_valid_span_id("0" * 16)

    def test_reject_non_string(self) -> None:
        assert not is_valid_span_id(None)
        assert not is_valid_span_id([])


# ---------------------------------------------------------------------------
# ContextVar：trace_id / parent_span_id
# ---------------------------------------------------------------------------


class TestTraceIdContextVar:
    def test_default_none(self) -> None:
        reset_trace_id()
        assert get_trace_id() is None

    def test_set_and_get(self) -> None:
        tid = generate_trace_id()
        set_trace_id(tid)
        assert get_trace_id() == tid
        reset_trace_id()

    def test_set_none_allowed(self) -> None:
        set_trace_id(None)
        assert get_trace_id() is None

    def test_set_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            set_trace_id("not-a-trace-id")
        with pytest.raises(ValueError):
            set_trace_id("0" * 32)

    def test_asyncio_isolation(self) -> None:
        """ContextVar 按协程上下文隔离。"""
        results: dict[str, str | None] = {}

        async def worker(name: str) -> None:
            set_trace_id(generate_trace_id())
            await asyncio.sleep(0.01)
            results[name] = get_trace_id()

        async def main() -> None:
            await asyncio.gather(worker("a"), worker("b"))

        asyncio.run(main())
        assert results["a"] != results["b"]


class TestParentSpanIdContextVar:
    def test_default_none(self) -> None:
        reset_parent_span_id()
        assert get_parent_span_id() is None

    def test_set_and_get(self) -> None:
        sid = generate_span_id()
        set_parent_span_id(sid)
        assert get_parent_span_id() == sid
        reset_parent_span_id()

    def test_set_none_allowed(self) -> None:
        set_parent_span_id(None)
        assert get_parent_span_id() is None

    def test_set_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            set_parent_span_id("bad")
        with pytest.raises(ValueError):
            set_parent_span_id("0" * 16)


# ---------------------------------------------------------------------------
# OTLP 常量与 now_unix_nano
# ---------------------------------------------------------------------------


class TestOtlpConstants:
    def test_span_kind_values(self) -> None:
        """数值对齐 OTLP proto。"""
        assert SpanKind.UNSPECIFIED == 0
        assert SpanKind.INTERNAL == 1
        assert SpanKind.SERVER == 2
        assert SpanKind.CLIENT == 3
        assert SpanKind.PRODUCER == 4
        assert SpanKind.CONSUMER == 5

    def test_status_code_values(self) -> None:
        assert StatusCode.UNSET == 0
        assert StatusCode.OK == 1
        assert StatusCode.ERROR == 2

    def test_attribute_keys(self) -> None:
        assert ATTR_SESSION_ID == "session.id"
        assert ATTR_CHANNEL == "channel.name"
        assert ATTR_CHAT_ID == "chat.id"
        assert ATTR_SENDER_ID == "sender.id"
        assert ATTR_TOOL_NAME == "tool.name"
        assert ATTR_TURN_ID == "turn.id"

    def test_now_unix_nano(self) -> None:
        before = time.time_ns()
        value = now_unix_nano()
        after = time.time_ns()
        assert isinstance(value, int)
        assert before <= value <= after


# ---------------------------------------------------------------------------
# traceparent format / parse
# ---------------------------------------------------------------------------


class TestTraceparent:
    def test_format_shape(self) -> None:
        tid, sid = generate_trace_id(), generate_span_id()
        tp = format_traceparent(tid, sid)
        assert tp == f"00-{tid}-{sid}-01"

    def test_format_lowercases_input(self) -> None:
        tid = "A" * 32
        sid = "B" * 16
        tp = format_traceparent(tid, sid)
        assert tp == f"00-{'a' * 32}-{'b' * 16}-01"

    def test_format_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            format_traceparent("bad", generate_span_id())
        with pytest.raises(ValueError):
            format_traceparent(generate_trace_id(), "bad")
        with pytest.raises(ValueError):
            format_traceparent(generate_trace_id(), generate_span_id(), flags="xyz")

    def test_round_trip(self) -> None:
        tid, sid = generate_trace_id(), generate_span_id()
        parsed = parse_traceparent(format_traceparent(tid, sid, flags="05"))
        assert parsed == (tid, sid, "05")

    def test_parse_none_on_garbage(self) -> None:
        assert parse_traceparent("") is None
        assert parse_traceparent("not-a-traceparent") is None
        assert parse_traceparent(None) is None  # type: ignore[arg-type]

    def test_parse_rejects_wrong_version(self) -> None:
        tid, sid = generate_trace_id(), generate_span_id()
        assert parse_traceparent(f"ff-{tid}-{sid}-01") is None

    def test_parse_rejects_wrong_field_count(self) -> None:
        tid, sid = generate_trace_id(), generate_span_id()
        assert parse_traceparent(f"00-{tid}-{sid}") is None
        assert parse_traceparent(f"00-{tid}-{sid}-01-extra") is None

    def test_parse_rejects_invalid_ids(self) -> None:
        assert parse_traceparent(f"00-{'z' * 32}-{'b' * 16}-01") is None
        assert parse_traceparent(f"00-{'a' * 32}-{'z' * 16}-01") is None
        assert parse_traceparent(f"00-{'0' * 32}-{'b' * 16}-01") is None
        assert parse_traceparent(f"00-{'a' * 32}-{'0' * 16}-01") is None

    def test_parse_rejects_invalid_flags(self) -> None:
        tid, sid = generate_trace_id(), generate_span_id()
        assert parse_traceparent(f"00-{tid}-{sid}-zz") is None
        assert parse_traceparent(f"00-{tid}-{sid}-001") is None

    def test_parse_accepts_flags_00(self) -> None:
        """flags=00（未采样）是合法值。"""
        tid, sid = generate_trace_id(), generate_span_id()
        assert parse_traceparent(f"00-{tid}-{sid}-00") == (tid, sid, "00")

    def test_parse_no_context_side_effect(self) -> None:
        """纯函数：parse 不触碰 ContextVar。"""
        reset_trace_id()
        reset_parent_span_id()
        tid, sid = generate_trace_id(), generate_span_id()
        parse_traceparent(format_traceparent(tid, sid))
        assert get_trace_id() is None
        assert get_parent_span_id() is None
