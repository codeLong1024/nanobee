"""工具执行中的违规/错误分类器。

将原始错误文本和异常类型映射为结构化的分类结果。
纯逻辑，无副作用，不持有状态。供 ToolPipeline 调用。
"""

from __future__ import annotations

from typing import Any

from nanobee.exceptions import SandboxViolationError
from nanobee.providers.base import ToolCallRequest
from nanobee.security.network import SSRF_BOUNDARY_NOTE
from nanobee.utils.logger import logger
from nanobee.utils.runtime import repeated_workspace_violation_error

# 分类结果三元组：(结果/消息, 事件字典, 致命异常)，None 表示未识别需透传
Classified = tuple[Any, dict[str, str], BaseException | None] | None


class FaultClassifier:
    """工具执行中的违规/错误分类器。

    不做工具特定的文本模式匹配，优先使用异常类型检测：
    - SandboxViolationError → 工作区越界
    - 文本包含框架模块输出特征 → SSRF 违规或工作区越界

    使用方式：
        classifier = FaultClassifier()
        handled = classifier.classify(
            raw_text=str(exc),
            soft_payload=payload,
            tool_call=tool_call,
            workspace_violation_counts=counts,
            exception=exc,
            exec_capable_tools=exec_tools,
        )
        if handled is not None:
            return handled
        # None 表示未识别，调用方自行处理
    """

    def classify(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
        exception: BaseException | None = None,
        exec_capable_tools: set[str] | None = None,
    ) -> Classified:
        """分类安全边界失败/工具错误，返回格式化结果或 None 以透传。

        Args:
            raw_text: 原始错误文本，用于模式匹配检测违规类型。
            soft_payload: 柔和版本的错误消息，返回给 LLM 供其自行修正。
            tool_call: 触发的工具调用（用于重复违规计数和升级提示）。
            workspace_violation_counts: 工作区违规计数，用于重复时升级。
            exception: 原始异常（None 表示从文本检测）。
            exec_capable_tools: 具有命令执行能力的工具名集合，用于升级提示。

        Returns:
            分类结果三元组，或 None 表示未识别需调用方自行处理。
        """
        # 异常类型检测（优先级最高）
        if isinstance(exception, SandboxViolationError):
            return self._handle_workspace(
                raw_text, soft_payload, tool_call, workspace_violation_counts,
                exec_capable_tools=exec_capable_tools,
            )

        # 文本特征检测——
        # SandboxViolationError.__str__() 始终输出 "沙箱拦截"
        # security/network.py 的 SSRF 错误始终包含 "私有/内网" 或 "private"
        if not raw_text:
            return None
        lowered = raw_text.lower()
        is_ssrf = "私有" in raw_text or "private" in lowered
        is_sandbox = "沙箱拦截" in raw_text

        if is_ssrf:
            return self._handle_ssrf(raw_text)

        if is_sandbox:
            return self._handle_workspace(
                raw_text, soft_payload, tool_call, workspace_violation_counts,
                exec_capable_tools=exec_capable_tools,
            )

        return None

    # =========================================================================
    # 私有处理方法
    # =========================================================================

    def _handle_ssrf(self, raw_text: str) -> tuple[Any, dict[str, str], None]:
        """处理 SSRF 违规：非可恢复，附 SSRF 边界提示。"""
        logger.warning(
            "Tool blocked by SSRF guard; returning non-retryable tool error: {}",
            raw_text.replace("\n", " ").strip()[:200],
        )
        event: dict[str, str] = {}
        event["detail"] = self._event_summary("ssrf_violation: ", raw_text)
        return self._ssrf_payload(raw_text), event, None

    @staticmethod
    def _handle_workspace(
        raw_text: str,
        soft_payload: str,
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
        exec_capable_tools: set[str] | None = None,
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """处理工作区越界：可恢复，重复时升级提示。"""
        escalation = repeated_workspace_violation_error(
            tool_call.name,
            tool_call.arguments,
            workspace_violation_counts,
            exec_capable_tools,
        )
        event: dict[str, str] = {}
        if escalation is not None:
            logger.warning(
                "Tool {} hit workspace boundary repeatedly; escalating hint",
                tool_call.name,
            )
            event["detail"] = FaultClassifier._event_summary(
                "workspace_violation_escalated: ",
                raw_text,
            )
            return escalation, event, None
        event["detail"] = FaultClassifier._event_summary("workspace_violation: ", raw_text)
        return soft_payload, event, None

    @classmethod
    def _ssrf_payload(cls, raw_text: str) -> str:
        """构造 SSRF 违规的柔和版消息。"""
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_summary(prefix: str, text: str, limit: int = 160) -> str:
        """生成事件摘要字符串。"""
        return (prefix + text.replace("\n", " ").strip())[:limit]
