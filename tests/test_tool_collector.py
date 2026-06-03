"""
ToolCollector 单元测试
"""

from __future__ import annotations

import pytest

from nanobee.kernel.tool_collector import ToolCollector


def test_all_tools_when_no_restrictions():
    """无白名单/黑名单时注册表中的全部可用"""
    collector = ToolCollector(["a", "b", "c"])
    assert collector.allowed_tools == ["a", "b", "c"]
    assert collector.is_allowed("a") is True
    assert collector.is_allowed("unknown") is False


def test_whitelist_filter():
    """白名单过滤"""
    collector = ToolCollector(
        ["a", "b", "c"],
        whitelist=["a", "b"],
    )
    assert sorted(collector.allowed_tools) == ["a", "b"]
    assert collector.is_allowed("a") is True
    assert collector.is_allowed("c") is False


def test_blacklist_filter():
    """黑名单过滤"""
    collector = ToolCollector(
        ["a", "b", "c"],
        blacklist=["b"],
    )
    assert sorted(collector.allowed_tools) == ["a", "c"]
    assert collector.is_allowed("b") is False


def test_whitelist_and_blacklist():
    """白名单 ∩ 黑名单取差集"""
    collector = ToolCollector(
        ["a", "b", "c", "d"],
        whitelist=["a", "b", "c"],
        blacklist=["b"],
    )
    assert sorted(collector.allowed_tools) == ["a", "c"]
    assert collector.is_allowed("b") is False
    assert collector.is_allowed("d") is False


def test_whitelist_nonexistent_tool():
    """白名单中的工具在注册表中不存在时自动忽略"""
    collector = ToolCollector(
        ["a", "b"],
        whitelist=["a", "nonexistent"],
    )
    assert collector.allowed_tools == ["a"]


def test_empty_whitelist_empty_blacklist():
    """空白名单+空黑名单 = 全部可用"""
    collector = ToolCollector(["a", "b"], whitelist=[], blacklist=[])
    assert collector.allowed_tools == ["a", "b"]


def test_none_whitelist_blacklist():
    """None 白名单/黑名单 = 全部可用"""
    collector = ToolCollector(["a", "b"], whitelist=None, blacklist=None)
    assert collector.allowed_tools == ["a", "b"]


def test_filter_definitions():
    """过滤 OpenAI 格式的工具定义"""
    collector = ToolCollector(["echo"], whitelist=["echo"])
    definitions = [
        {"type": "function", "function": {"name": "echo", "description": "Echo"}},
        {"type": "function", "function": {"name": "secret", "description": "Secret"}},
    ]
    result = collector.filter_definitions(definitions)
    assert len(result) == 1
    assert result[0]["function"]["name"] == "echo"


def test_filter_definitions_flat_schema():
    """支持扁平 schema 格式"""
    collector = ToolCollector(["tool-a"], whitelist=["tool-a"])
    definitions = [
        {"name": "tool-a", "description": "Tool A"},
        {"name": "tool-b", "description": "Tool B"},
    ]
    result = collector.filter_definitions(definitions)
    assert len(result) == 1
    assert result[0]["name"] == "tool-a"


def test_has_restrictions():
    """has_restrictions 正确反映限制状态"""
    assert ToolCollector(["a"]).has_restrictions is False
    assert ToolCollector(["a"], whitelist=["a"]).has_restrictions is True
    assert ToolCollector(["a"], blacklist=["b"]).has_restrictions is True


def test_repr():
    """repr 包含统计信息"""
    collector = ToolCollector(["a", "b", "c"], whitelist=["a", "b"])
    rep = repr(collector)
    assert "allowed=2/3" in rep
    assert "whitelist=2" in rep
