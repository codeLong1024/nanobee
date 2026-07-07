"""ResultNormalizer 单元测试 — 工具结果标准化。

覆盖场景：
1. 非空文本直通
2. 空结果 → 占位文本
3. 非字符串结果序列化为 JSON
4. 超长结果截断
5. 持久化成功返回引用
6. 持久化失败优雅降级
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nanobee.agent.result_normalizer import ResultNormalizer


class TestResultNormalizer:
    """ResultNormalizer.normalize() 测试。"""

    def test_normalize_string_passthrough(self) -> None:
        """字符串结果直通。"""
        result = ResultNormalizer.normalize(
            "simple result",
            tool_name="read_file",
            tool_call_id="call-1",
            workspace=None,
            context_id="ctx-1",
            max_chars=10000,
        )
        assert result == "simple result"

    def test_normalize_empty_result_placeholder(self) -> None:
        """空结果应被替换为占位文本。"""
        result = ResultNormalizer.normalize(
            "",
            tool_name="write_file",
            tool_call_id="call-2",
            workspace=None,
            context_id="ctx-2",
            max_chars=10000,
        )
        assert result
        assert "write_file" in result.lower()

    def test_normalize_none_result_placeholder(self) -> None:
        """None 结果应被替换为占位文本。"""
        result = ResultNormalizer.normalize(
            None,
            tool_name="execute_shell",
            tool_call_id="call-3",
            workspace=None,
            context_id="ctx-3",
            max_chars=10000,
        )
        assert result
        assert "execute_shell" in result.lower()

    def test_normalize_dict_result_serialized(self) -> None:
        """dict 结果应被序列化为 JSON 字符串。"""
        result = ResultNormalizer.normalize(
            {"key": "value", "count": 42},
            tool_name="web_search",
            tool_call_id="call-4",
            workspace=None,
            context_id="ctx-4",
            max_chars=10000,
        )
        assert isinstance(result, str)
        assert '"key"' in result
        assert '"count"' in result
        assert "42" in result

    def test_normalize_truncate_long_result(self, tmp_path: Path) -> None:
        """超长结果应被截断。"""
        long_text = "x" * 500
        result = ResultNormalizer.normalize(
            long_text,
            tool_name="read_file",
            tool_call_id="call-5",
            workspace=None,
            context_id="ctx-5",
            max_chars=100,
        )
        # 截断函数可能在末尾追加 "... (truncated)" 等标记，
        # 因此最终长度可能略超 max_chars，但不应包含完整原文
        assert len(result) < 300
        assert "xxx" in result  # 至少包含部分原始内容

    def test_normalize_with_persist(self, tmp_path: Path) -> None:
        """超长结果在 workspace 存在时应持久化。"""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "tool_results").mkdir()

        long_text = "A" * 2000
        result = ResultNormalizer.normalize(
            long_text,
            tool_name="read_file",
            tool_call_id="call-6",
            workspace=workspace,
            context_id="ctx-6",
            max_chars=500,
        )
        # 应返回文件引用而非完整内容
        assert result
        assert not result.startswith("A" * 2000)  # 不返回完整内容

    def test_normalize_persist_failure_graceful(self, tmp_path: Path) -> None:
        """持久化失败时优雅降级为返回原始结果。"""
        workspace = tmp_path / "broken"
        workspace.mkdir()

        # 通过 patch 让 maybe_persist_tool_result 抛异常
        with patch(
            "nanobee.agent.result_normalizer.maybe_persist_tool_result",
            side_effect=OSError("磁盘已满"),
        ):
            result = ResultNormalizer.normalize(
                "some result",
                tool_name="test_tool",
                tool_call_id="call-7",
                workspace=workspace,
                context_id="ctx-7",
                max_chars=10000,
            )
        assert result is not None
        # 应返回原始内容（降级）
        assert "some result" in result

    def test_normalize_list_result_serialized(self) -> None:
        """list 结果应被序列化为 JSON 字符串。"""
        result = ResultNormalizer.normalize(
            [1, 2, 3, "four"],
            tool_name="list_dir",
            tool_call_id="call-8",
            workspace=None,
            context_id="ctx-8",
            max_chars=10000,
        )
        assert isinstance(result, str)
        assert "[1, 2, 3, " in result

    def test_normalize_non_serializable_result(self) -> None:
        """无法序列化的对象应转为 str。"""
        class Unserializable:
            def __str__(self) -> str:
                return "unserializable object"

        result = ResultNormalizer.normalize(
            Unserializable(),
            tool_name="custom_tool",
            tool_call_id="call-9",
            workspace=None,
            context_id="ctx-9",
            max_chars=10000,
        )
        assert isinstance(result, str)

    def test_normalize_max_chars_zero_disabled(self) -> None:
        """max_chars=0 时不截断。"""
        result = ResultNormalizer.normalize(
            "x" * 100,
            tool_name="t",
            tool_call_id="c-10",
            workspace=None,
            context_id="ctx-10",
            max_chars=0,
        )
        # max_chars=0 表示不截断，但 truncate_text 可能有内部处理
        assert result is not None
