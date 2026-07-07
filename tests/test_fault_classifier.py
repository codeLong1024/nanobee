"""FaultClassifier 单元测试 — 工具执行中的违规/错误分类器。

覆盖场景：
1. SandboxViolationError 异常检测 → workspace 分类
2. SSRF 文本特征检测（"私有"/"private" 关键词）
3. 沙箱拦截文本特征检测（"沙箱拦截"）
4. 空/无文本 → None 透传
5. 重复违规升级提示
6. _event_summary 截断逻辑
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobee.agent.fault_classifier import FaultClassifier
from nanobee.exceptions import SandboxViolationError
from nanobee.providers.base import ToolCallRequest


class TestFaultClassifierClassify:
    """FaultClassifier.classify() 测试。"""

    def test_sandbox_violation_exception_classified(self) -> None:
        """SandboxViolationError 异常应归类为 workspace 违规。"""
        classifier = FaultClassifier()
        exc = SandboxViolationError("/bad/path", "/root", detail="越界路径")
        tool_call = ToolCallRequest(id="call-1", name="read_file", arguments={"path": "/bad/path"})

        result = classifier.classify(
            raw_text=str(exc),
            soft_payload="柔和版消息",
            tool_call=tool_call,
            workspace_violation_counts={},
            exception=exc,
        )
        assert result is not None
        result_text, event, fatal = result
        assert "柔和版消息" in result_text
        assert fatal is None  # 非致命

    def test_sandbox_violation_repeated_escalates(self) -> None:
        """重复 workspace 违规应触发升级提示。"""
        classifier = FaultClassifier()
        exc = SandboxViolationError("/bad/path", "/root", detail="越界路径")
        tool_call = ToolCallRequest(
            id="call-2", name="execute_shell",
            arguments={"command": "ls /bad", "working_dir": "/bad"},
        )

        # 预填充多次相同的违规签名以触发升级阈值
        result = classifier.classify(
            raw_text=str(exc),
            soft_payload="柔和版消息",
            tool_call=tool_call,
            workspace_violation_counts={
                "violation:/bad/path": 10,  # 超过阈值触发升级
            },
            exception=exc,
            exec_capable_tools={"execute_shell"},
        )
        assert result is not None
        result_text, event, fatal = result
        # 升级后消息应不再只是原始柔和版
        assert fatal is None  # 不是致命错误

    def test_ssrf_text_detected(self) -> None:
        """包含"私有"关键词的错误文本应归类为 SSRF。"""
        classifier = FaultClassifier()
        tool_call = ToolCallRequest(id="call-3", name="web_fetch", arguments={"url": "http://10.0.0.1"})

        result = classifier.classify(
            raw_text="请求被阻止：目标地址为私有地址",
            soft_payload="无法访问",
            tool_call=tool_call,
            workspace_violation_counts={},
        )
        assert result is not None
        result_text, event, fatal = result
        assert "SSRF" in result_text or "私有" in result_text
        assert fatal is None  # SSRF 也非致命

    def test_ssrf_text_english_detected(self) -> None:
        """包含"private"关键词的英文错误文本应归类为 SSRF。"""
        classifier = FaultClassifier()
        tool_call = ToolCallRequest(id="call-4", name="web_fetch", arguments={"url": "http://internal"})

        result = classifier.classify(
            raw_text="Request blocked: destination is a private IP",
            soft_payload="Unable to access",
            tool_call=tool_call,
            workspace_violation_counts={},
        )
        assert result is not None
        result_text, event, fatal = result
        assert "private" in result_text.lower()
        assert fatal is None

    def test_sandbox_text_detected(self) -> None:
        """包含"沙箱拦截"关键词的错误文本应归类为 workspace 违规。"""
        classifier = FaultClassifier()
        tool_call = ToolCallRequest(id="call-5", name="write_file", arguments={"path": "../outside"})

        result = classifier.classify(
            raw_text="沙箱拦截：路径超出允许范围",
            soft_payload="不能写入此路径",
            tool_call=tool_call,
            workspace_violation_counts={},
        )
        assert result is not None
        result_text, event, fatal = result
        assert "不能写入此路径" in result_text
        assert fatal is None

    def test_empty_text_returns_none(self) -> None:
        """空错误文本应返回 None（透传给调用方处理）。"""
        classifier = FaultClassifier()
        tool_call = ToolCallRequest(id="call-6", name="unknown_tool", arguments={})

        result = classifier.classify(
            raw_text="",
            soft_payload="未知错误",
            tool_call=tool_call,
            workspace_violation_counts={},
        )
        assert result is None

    def test_unrecognized_text_returns_none(self) -> None:
        """无法识别的错误文本应返回 None。"""
        classifier = FaultClassifier()
        tool_call = ToolCallRequest(id="call-7", name="web_search", arguments={"query": "test"})

        result = classifier.classify(
            raw_text="Some generic error occurred",
            soft_payload="发生错误",
            tool_call=tool_call,
            workspace_violation_counts={},
        )
        assert result is None

    def test_exception_takes_priority_over_text(self) -> None:
        """异常类型检测优先于文本特征检测。"""
        classifier = FaultClassifier()
        exc = SandboxViolationError("/bad", "/root")
        tool_call = ToolCallRequest(
            id="call-8", name="web_fetch", arguments={"path": "/bad"},
        )

        # 同时提供 SSRF 文本和 SandboxViolationError 异常 → 异常优先
        result = classifier.classify(
            raw_text="Request blocked: private IP address",  # SSRF 文本
            soft_payload="柔和消息",
            tool_call=tool_call,
            workspace_violation_counts={},
            exception=exc,  # SandboxViolationError 应优先
            exec_capable_tools=frozenset(),  # 非空集合避免 None 迭代
        )
        assert result is not None
        result_text, _, _ = result
        # 应走 workspace 分支（异常优先），不是 SSRF 分支
        assert "柔和消息" in result_text


class TestFaultClassifierSSRFPayload:
    """FaultClassifier._ssrf_payload() 测试。"""

    def test_ssrf_payload_appends_boundary_note(self) -> None:
        """SSRF 负载应附加边界提示。"""
        payload = FaultClassifier._ssrf_payload("请求被阻止：内网 IP")
        assert "请求被阻止" in payload
        assert "non-bypassable" in payload

    def test_ssrf_payload_empty_text(self) -> None:
        """空文本时使用默认消息。"""
        payload = FaultClassifier._ssrf_payload("")
        assert "Error:" in payload or "blocked" in payload


class TestFaultClassifierEventSummary:
    """FaultClassifier._event_summary() 测试。"""

    def test_event_summary_basic(self) -> None:
        """事件摘要正确拼接前缀和截断。"""
        summary = FaultClassifier._event_summary("PREFIX: ", "test message")
        assert summary == "PREFIX: test message"

    def test_event_summary_truncation(self) -> None:
        """超长文本被截断到 limit。"""
        long_text = "x" * 200
        summary = FaultClassifier._event_summary("P: ", long_text, limit=20)
        assert len(summary) <= 20
        assert summary.startswith("P: ")

    def test_event_summary_newline_replace(self) -> None:
        """换行符被替换为空格。"""
        summary = FaultClassifier._event_summary("E: ", "line1\nline2\nline3")
        assert "\n" not in summary
        assert "line1 line2 line3" in summary
