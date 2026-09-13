"""utils/redact.py 单元测试 — 错误串归一化与凭证脱敏。

覆盖场景：
1. redact_secrets：URL query / Authorization 头 / snake_case 复合键 / JSON 引号键
   四类凭证形态的掩码；普通文本不被误伤；前置边界不误配（monkey=）。
2. normalize_error：异常对象、空 message 异常、各类既有前缀（Error: / Exception: /
   中文冒号）的剥离；无前缀文本与空文本的处理；归一化 + 脱敏的串联。
"""

from __future__ import annotations

import pytest

from nanobee.utils.redact import normalize_error, redact_secrets


class TestRedactSecrets:
    """凭证值掩码（键名保留）。"""

    def test_plain_text_untouched(self) -> None:
        """无凭证形态的普通诊断原样返回。"""
        assert redact_secrets("LLM 调用失败：超时") == "LLM 调用失败：超时"

    def test_url_query_key_redacted(self) -> None:
        """URL query 形态：httpx 异常 str 的典型泄漏面。"""
        secret = "fakekeyfakekeyfakekey"
        text = f"for url 'https://mcp-gw.example.com/server/abc?key={secret}'"
        out = redact_secrets(text)
        assert secret not in out
        assert "key=<redacted>" in out

    def test_query_value_stops_at_ampersand(self) -> None:
        """值边界止于 &: 后续参数不被吞。"""
        out = redact_secrets("?key=abc123&next=1")
        assert out == "?key=<redacted>&next=1"

    def test_authorization_bearer_redacted(self) -> None:
        """Authorization 头形态（含 Bearer 前缀）。"""
        text = "request failed with Authorization: Bearer sk-ant-api-xyz123"
        out = redact_secrets(text)
        assert "sk-ant-api-xyz123" not in out
        assert "Authorization: Bearer <redacted>" in out

    @pytest.mark.parametrize(
        "text",
        [
            "client_secret=abcabcabcabc",
            "db_password=passwordpassword",
            "refresh_token=tokentokentoken",
        ],
    )
    def test_snake_case_composite_key_redacted(self, text: str) -> None:
        """回归：初版 \\b 前置边界在 _ 两侧不成立，snake_case 复合键漏掩码。"""
        out = redact_secrets(text)
        assert "<redacted>" in out
        assert text.split("=", 1)[1] not in out

    def test_json_quoted_key_and_value_redacted(self) -> None:
        """回归：JSON 形态（键与值均带引号）初版失配。"""
        out = redact_secrets('{"api_key": "sk-abc123", "x": 1}')
        assert "sk-abc123" not in out
        assert '"api_key": "<redacted>"' in out
        assert '"x": 1' in out

    def test_case_insensitive(self) -> None:
        """键名大小写不敏感。"""
        assert "SECRET123" not in redact_secrets("API_KEY=SECRET123")

    def test_prefix_boundary_avoids_false_positive(self) -> None:
        """前置非字母数字边界：普通单词内嵌 key 不误伤。"""
        assert redact_secrets("monkey=banana") == "monkey=banana"


class TestNormalizeError:
    """错误串归一（统一无前缀形态）+ 脱敏。"""

    def test_exception_object(self) -> None:
        """异常对象归一为 <类型>: <正文>，不带 Error: 前缀。"""
        assert normalize_error(RuntimeError("LLM 调用失败")) == "RuntimeError: LLM 调用失败"

    def test_exception_with_empty_message(self) -> None:
        """空 message 的异常只保留类型名（不留悬空冒号）。"""
        assert normalize_error(RuntimeError()) == "RuntimeError"

    def test_error_prefix_stripped_keeping_type(self) -> None:
        """剥离 Error: 前缀但保留异常类型（runner 旧形态归一后的结果）。"""
        assert normalize_error("Error: RuntimeError: 连接超时") == "RuntimeError: 连接超时"

    @pytest.mark.parametrize(
        "text",
        [
            "error: 连接超时",
            "Exception: 连接超时",
            "ERROR：连接超时",
        ],
    )
    def test_existing_prefix_stripped(self, text: str) -> None:
        """既有前缀（英文/中文冒号、大小写）统一剥离。"""
        assert normalize_error(text) == "连接超时"

    def test_plain_text_untouched(self) -> None:
        """无前缀文本原样返回。"""
        assert normalize_error("LLM 调用失败：模型返回错误") == "LLM 调用失败：模型返回错误"

    def test_empty_text_returns_empty(self) -> None:
        """空文本返回空串——由调用方决定兜底文案。"""
        assert normalize_error("") == ""

    def test_non_prefix_english_sentence_kept(self) -> None:
        """'Error calling LLM: x' 非前缀形态（冒号不紧随 Error），不被误剥离。"""
        assert normalize_error("Error calling LLM: timeout") == "Error calling LLM: timeout"

    def test_redaction_applied_within_normalization(self) -> None:
        """归一化与脱敏同源：provider 前缀 + URL 密钥一次性处理。"""
        secret = "fakekeyfakekeyfakekey"
        out = normalize_error(f"Error: HTTPStatusError: 500 for url 'http://x?key={secret}'")
        assert out.startswith("HTTPStatusError:")
        assert secret not in out
        assert "<redacted>" in out
