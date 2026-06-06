"""Tests for the unified exception hierarchy in nanobee.exceptions."""
# pylint: disable=too-few-public-methods,redefined-outer-name

from __future__ import annotations

import pytest

from nanobee.exceptions import (
    AgentError,
    ArtifactError,
    CodexHTTPError,
    ContextError,
    ImageGenerationError,
    LoopStateError,
    NanobeeError,
    PluginError,
    PluginNotFoundError,
    ProviderAPIError,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderTimeoutError,
    RouteError,
    SandboxViolationError,
    SoulViolationError,
    StorageError,
    ToolExecutionError,
    ToolViolationError,
    UnknownRouteError,
)


class TestNanobeeErrorBase:
    """NanobeeError 基类行为验证"""

    def test_basic_instantiation(self) -> None:
        err = NanobeeError("test message")
        assert str(err) == "test message"
        assert err.detail == {}

    def test_with_detail(self) -> None:
        err = NanobeeError("msg", detail={"key": "val"})
        assert err.detail == {"key": "val"}

    def test_default_message(self) -> None:
        err = NanobeeError()
        assert str(err) == ""


class TestHierarchy:
    """验证所有自定义异常均为 NanobeeError 子类"""

    @pytest.mark.parametrize("exc_cls", [
        PluginError,
        PluginNotFoundError,
        RouteError,
        UnknownRouteError,
        SandboxViolationError,
        SoulViolationError,
        ContextError,
        ProviderError,
        ProviderConfigError,
        ProviderAuthError,
        ProviderTimeoutError,
        ProviderAPIError,
        ImageGenerationError,
        CodexHTTPError,
        AgentError,
        ToolExecutionError,
        ToolViolationError,
        LoopStateError,
        StorageError,
        ArtifactError,
    ])
    def test_is_nanobeer_error(self, exc_cls: type) -> None:
        assert issubclass(exc_cls, NanobeeError)

    def test_catch_all(self) -> None:
        """验证 except NanobeeError 能捕获所有子类异常"""
        all_exc_types: list[type] = [
            PluginError,
            UnknownRouteError,  # 需要 channel + chat_id
            SandboxViolationError,
            SoulViolationError,
            ContextError,
            ProviderConfigError,
            ProviderAuthError,
            ProviderTimeoutError,
            ProviderAPIError,
            ImageGenerationError,
            CodexHTTPError,
            ToolExecutionError,
            ToolViolationError,
            LoopStateError,
            ArtifactError,
        ]

        # 为需要特殊参数的异常准备参数字典
        args_map: dict[type, tuple] = {
            UnknownRouteError: ("cli", "chat-123"),
            SandboxViolationError: ("/bad/path", "/root"),
        }

        for exc_type in all_exc_types:
            args = args_map.get(exc_type, ("test",))
            try:
                raise exc_type(*args)
            except NanobeeError:
                pass
            else:
                pytest.fail(f"{exc_type.__name__} was not caught by except NanobeeError")

    def test_catch_specific_then_base(self) -> None:
        """先捕获具体类型，再 fallback 到 NanobeeError"""
        try:
            raise ProviderConfigError("missing key")
        except ProviderError:
            pass
        except NanobeeError:
            pytest.fail("specific catch should have precedence")


class TestUnknownRouteError:
    def test_constructor(self) -> None:
        err = UnknownRouteError("cli", "chat-123")
        assert err.channel == "cli"
        assert err.chat_id == "chat-123"
        assert "cli" in str(err)
        assert "chat-123" in str(err)
        assert err.detail == {"channel": "cli", "chat_id": "chat-123"}

    def test_is_route_error(self) -> None:
        assert issubclass(UnknownRouteError, RouteError)


class TestSandboxViolationError:
    def test_default_detail(self) -> None:
        err = SandboxViolationError("/bad/path", "/root")
        assert err.path == "/bad/path"
        assert err.context_root == "/root"
        assert "越界" not in str(err)

    def test_with_sub_detail(self) -> None:
        err = SandboxViolationError("/bad/path", "/root", detail="路径逃逸拦截")
        assert "路径逃逸拦截" in str(err)
        assert err.detail["sub_detail"] == "路径逃逸拦截"


class TestProviderErrors:
    def test_provider_api_error(self) -> None:
        err = ProviderAPIError("bad request", status_code=400, body="invalid param")
        assert err.status_code == 400
        assert err.body == "invalid param"
        assert err.detail == {"status_code": 400, "body": "invalid param"}

    def test_codex_http_error(self) -> None:
        err = CodexHTTPError("rate limited", status_code=429, retry_after=60.0)
        assert err.status_code == 429
        assert err.retry_after == 60.0
        assert "rate limited" in str(err)

    def test_image_generation_error_inheritance(self) -> None:
        assert issubclass(ImageGenerationError, ProviderError)


class TestAgentErrors:
    def test_loop_state_error(self) -> None:
        err = LoopStateError("无此状态处理器: SPLIT")
        assert "无此状态处理器" in str(err)

    def test_tool_execution_error(self) -> None:
        assert issubclass(ToolExecutionError, AgentError)

    def test_tool_violation_error(self) -> None:
        assert issubclass(ToolViolationError, AgentError)


class TestStorageErrors:
    def test_artifact_error_inheritance(self) -> None:
        assert issubclass(ArtifactError, StorageError)


def test_import_from_kernel() -> None:
    """验证 kernel/__init__.py 对外导出统一异常"""
    import nanobee.kernel as kernel_mod

    assert kernel_mod.NanobeeError is NanobeeError
    assert kernel_mod.PluginError is PluginError
    assert kernel_mod.UnknownRouteError is UnknownRouteError
    assert kernel_mod.SandboxViolationError is SandboxViolationError
    assert kernel_mod.SoulViolationError is SoulViolationError
    assert kernel_mod.ContextError is ContextError


def test_import_from_providers() -> None:
    """验证 providers/__init__.py 对外导出统一异常"""
    import nanobee.providers as providers_mod

    assert providers_mod.ProviderError is ProviderError
    assert providers_mod.ProviderConfigError is ProviderConfigError
    assert providers_mod.ProviderAuthError is ProviderAuthError
    assert providers_mod.ProviderTimeoutError is ProviderTimeoutError
    assert providers_mod.ProviderAPIError is ProviderAPIError
    assert providers_mod.ImageGenerationError is ImageGenerationError
    assert providers_mod.CodexHTTPError is CodexHTTPError


def test_import_from_agent() -> None:
    """验证 agent/__init__.py 对外导出统一异常"""
    import nanobee.agent as agent_mod

    assert agent_mod.AgentError is AgentError
    assert agent_mod.LoopStateError is LoopStateError
    assert agent_mod.ToolExecutionError is ToolExecutionError
    assert agent_mod.ToolViolationError is ToolViolationError


def test_import_from_utils() -> None:
    """验证 utils/__init__.py 对外导出统一异常"""
    import nanobee.utils as utils_mod

    assert utils_mod.NanobeeError is NanobeeError
    assert utils_mod.StorageError is StorageError
    assert utils_mod.ArtifactError is ArtifactError


def test_sandbox_backward_compat() -> None:
    """验证 sandbox.py 中 SandboxError 别名可用"""
    from nanobee.kernel.sandbox import SandboxError

    assert SandboxError is SandboxViolationError
    err = SandboxError("/p", "/root")
    assert isinstance(err, SandboxViolationError)
    assert isinstance(err, NanobeeError)
