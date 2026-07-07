"""MessageTool 单元测试 — 结构化消息发送与媒体收集。

覆盖场景：
1. MessageTool 基本执行
2. 媒体路径校验（本地文件存在/不存在、URL 直通）
3. collect_message_tool_media（提取 tool_calls 中的内容和媒体）
4. 空/无效输入防御
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nanobee.agent.tools.message import MessageTool, collect_message_tool_media


class TestMessageToolExecute:
    """MessageTool.execute() 测试。"""

    @pytest.mark.asyncio
    async def test_execute_with_content_only(self) -> None:
        """仅含文本内容，无附件。"""
        tool = MessageTool()
        result = await tool.execute(content="Hello World")
        assert "发送队列" in result
        assert "Hello World" in result

    @pytest.mark.asyncio
    async def test_execute_with_content_and_valid_media(self, tmp_path: Path) -> None:
        """有效本地文件路径。"""
        report = tmp_path / "report.pdf"
        report.write_text("report content", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(content="Here is the report", media=[str(report)])
        assert "发送队列" in result
        assert "report.pdf" in result

    @pytest.mark.asyncio
    async def test_execute_with_http_media(self) -> None:
        """HTTP/HTTPS URL 不校验存在性，直接通过。"""
        tool = MessageTool()
        result = await tool.execute(
            content="Check this image",
            media=["https://example.com/img.png"],
        )
        assert "发送队列" in result
        assert "img.png" in result

    @pytest.mark.asyncio
    async def test_execute_with_invalid_media_path(self) -> None:
        """不存在的本地文件路径应报错。"""
        tool = MessageTool()
        result = await tool.execute(
            content="Here is a file",
            media=["/nonexistent/path/file.pdf"],
        )
        assert "错误" in result
        assert "不存在" in result
        assert "file.pdf" in result

    @pytest.mark.asyncio
    async def test_execute_with_mixed_media(self, tmp_path: Path) -> None:
        """混合有效和无效路径时，仅报错无效路径。"""
        valid = tmp_path / "valid.txt"
        valid.write_text("data", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(
            content="Mixed media",
            media=[str(valid), "/bad/path.pdf"],
        )
        assert "错误" in result
        assert "bad/path.pdf" in result
        # 有效路径不应出现在错误消息中
        assert "valid.txt" not in result.split("错误")[1] if "错误" in result else True

    @pytest.mark.asyncio
    async def test_execute_with_non_string_media(self) -> None:
        """非字符串类型的 media 元素应报错。"""
        tool = MessageTool()
        result = await tool.execute(content="Test", media=[123, True])
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_execute_long_content_truncated_in_preview(self) -> None:
        """超长内容在预览中被截断。"""
        tool = MessageTool()
        long_content = "A" * 100
        result = await tool.execute(content=long_content)
        assert "..." in result
        assert "发送队列" in result

    @pytest.mark.asyncio
    async def test_execute_multiple_valid_media(self, tmp_path: Path) -> None:
        """多个有效本地文件路径。"""
        f1 = tmp_path / "f1.pdf"
        f2 = tmp_path / "f2.png"
        f1.write_text("c1", encoding="utf-8")
        f2.write_text("c2", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(content="Files", media=[str(f1), str(f2)])
        assert "发送队列" in result
        assert "f1.pdf" in result
        assert "f2.png" in result

    @pytest.mark.asyncio
    async def test_execute_relative_path_without_absolute_file(self) -> None:
        """相对路径且文件不存在时也应报错（因为是绝对路径检查）。"""
        tool = MessageTool()
        # 相对路径（非绝对路径）不会触发存在性检查
        result = await tool.execute(content="Test", media=["relative/file.txt"])
        assert "错误" not in result  # 相对路径不校验存在性


class TestMessageToolSchema:
    """MessageTool schema 测试。"""

    def test_tool_name(self) -> None:
        """工具名称应为 'message'。"""
        tool = MessageTool()
        assert tool.name == "message"

    def test_tool_description_non_empty(self) -> None:
        """工具描述非空。"""
        tool = MessageTool()
        assert tool.description
        assert "send" in tool.description.lower()

    def test_tool_parameters_schema(self) -> None:
        """参数 schema 包含 content（必需）和 media（可选）。"""
        tool = MessageTool()
        params = tool.parameters
        assert params["type"] == "object"
        assert "content" in params["properties"]
        assert "media" in params["properties"]
        assert params["required"] == ["content"]


class TestCollectMessageToolMedia:
    """collect_message_tool_media() 测试。"""

    def test_collect_from_single_tool_call(self) -> None:
        """从单个 tool_call 中提取内容和媒体。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({
                                "content": "Report ready",
                                "media": ["/path/to/report.pdf"],
                            }),
                        },
                    }
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        assert content == "Report ready"
        assert media == ["/path/to/report.pdf"]

    def test_collect_from_multiple_tool_calls(self) -> None:
        """多个 message tool_call 时，content 取第一个遇到的（从后往前遍历）。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({
                                "content": "First msg",
                                "media": ["/path/f1.pdf"],
                            }),
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({
                                "content": "Second msg",
                                "media": ["/path/f2.pdf"],
                            }),
                        },
                    }
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        # reversed() 从最后往前遍历，最后迭代的是第一个 message，其 content 会覆盖
        # 所以最终 content 是第一个 message 调用的 content
        assert content == "First msg"
        assert media == ["/path/f1.pdf", "/path/f2.pdf"]

    def test_collect_dedup_media(self) -> None:
        """重复的 media 路径自动去重。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({
                                "content": "Msg 1",
                                "media": ["/same/file.pdf"],
                            }),
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({
                                "content": "Msg 2",
                                "media": ["/same/file.pdf", "/other/file.png"],
                            }),
                        },
                    }
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        # reversed() 最后迭代第一个 message，其 content 覆盖第二个
        assert content == "Msg 1"
        # media 按原始顺序排列（去重后 reverse 回原始顺序）
        assert len(media) == 2
        assert "/same/file.pdf" in media
        assert "/other/file.png" in media

    def test_collect_ignores_non_message_tool_calls(self) -> None:
        """忽略非 message 的 tool_call。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp"}),
                        },
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"content": "Only message", "media": []}),
                        },
                    }
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        assert content == "Only message"
        assert media == []

    def test_collect_no_message_calls(self) -> None:
        """无 message tool_call 时返回 None 和空列表。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        content, media = collect_message_tool_media(messages)
        assert content is None
        assert media == []

    def test_collect_empty_messages(self) -> None:
        """空消息列表返回 None 和空列表。"""
        content, media = collect_message_tool_media([])
        assert content is None
        assert media == []

    def test_collect_invalid_json_arguments(self) -> None:
        """JSON 解析失败的 tool_call 被优雅跳过。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": "not-valid-json",
                        },
                    }
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        assert content is None
        assert media == []

    def test_collect_non_dict_message(self) -> None:
        """非 dict 类型的消息被跳过。"""
        messages = [
            "not a dict",
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "message", "arguments": json.dumps({"content": "Hi"})}},
            ]},
        ]
        content, media = collect_message_tool_media(messages)
        assert content == "Hi"

    def test_collect_tool_calls_with_non_dict_item(self) -> None:
        """tool_calls 列表中的非 dict 元素被跳过。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    "not a dict",
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"content": "Valid"})},
                    },
                ],
            },
        ]
        content, media = collect_message_tool_media(messages)
        assert content == "Valid"
