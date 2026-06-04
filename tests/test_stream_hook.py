"""Test _StreamHook interface compliance.

Verifies that _StreamHook implements the full AgentHook interface
so CompositeHook._for_each_hook_safe will not raise AttributeError.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobee.kernel.kernel import _StreamHook


# All AgentHook lifecycle methods that CompositeHook._for_each_hook_safe
# may dispatch to (including finalize_content which is called directly).
_LIFECYCLE_METHODS: tuple[str, ...] = (
    "before_iteration",
    "on_stream",
    "on_stream_end",
    "after_iteration",
    "before_execute_tools",
    "emit_reasoning",
    "emit_reasoning_end",
    "finalize_content",
)


class TestStreamHookInterface:
    """Verify _StreamHook implements all AgentHook lifecycle methods."""

    @pytest.mark.parametrize("method_name", _LIFECYCLE_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        """Every AgentHook lifecycle method must exist and be callable."""
        hook = _StreamHook()
        assert hasattr(hook, method_name)
        assert callable(getattr(hook, method_name))

    @pytest.mark.parametrize("method_name", _LIFECYCLE_METHODS)
    @pytest.mark.asyncio
    async def test_async_method_can_be_called_safely(self, method_name: str) -> None:
        """Every async lifecycle method must not raise when called.

        Skip sync methods (wants_streaming, finalize_content).
        """
        sync_methods = ("wants_streaming", "finalize_content")
        if method_name in sync_methods:
            return
        hook = _StreamHook()
        method = getattr(hook, method_name)
        ctx: Any = object()
        kwargs: dict[str, Any] = {}
        if method_name in ("on_stream",):
            kwargs["delta"] = ""
        elif method_name == "on_stream_end":
            kwargs["resuming"] = False
        elif method_name == "emit_reasoning":
            await method(None)
            return
        elif method_name in ("emit_reasoning_end",):
            # emit_reasoning_end takes no args (except self)
            await method()
            return
        await method(ctx, **kwargs)
        # no exception = pass


class TestStreamHookBehavior:
    """Verify correct behavior of _StreamHook methods."""

    def test_finalize_content_pass_through(self) -> None:
        """finalize_content returns content unchanged."""
        hook = _StreamHook()
        content = "test content"
        result = hook.finalize_content(None, content)
        assert result is content

    def test_finalize_content_none(self) -> None:
        """finalize_content handles None content."""
        hook = _StreamHook()
        result = hook.finalize_content(None, None)
        assert result is None

    def test_wants_streaming_without_callback(self) -> None:
        """wants_streaming returns False when no on_stream provided."""
        hook = _StreamHook()
        assert hook.wants_streaming() is False

    def test_wants_streaming_with_callback(self) -> None:
        """wants_streaming returns True when on_stream provided."""
        async def dummy(delta: str) -> None:
            pass
        hook = _StreamHook(on_stream=dummy)
        assert hook.wants_streaming() is True

    @pytest.mark.asyncio
    async def test_on_stream_called_with_delta(self) -> None:
        """on_stream invokes the registered callback."""
        received: list[str] = []

        async def capture(delta: str) -> None:
            received.append(delta)

        hook = _StreamHook(on_stream=capture)
        await hook.on_stream(None, "hello")
        assert received == ["hello"]

    @pytest.mark.asyncio
    async def test_on_stream_skips_empty_delta(self) -> None:
        """on_stream does not call back for empty delta."""

        def _should_not_be_called(_: str) -> None:
            msg = "should not be called"
            raise AssertionError(msg)

        hook = _StreamHook(on_stream=_should_not_be_called)
        await hook.on_stream(None, "")
        # no exception = pass

    @pytest.mark.asyncio
    async def test_on_stream_without_callback_is_noop(self) -> None:
        """on_stream is noop when no callback registered."""
        hook = _StreamHook()
        await hook.on_stream(None, "data")
        # no exception = pass
