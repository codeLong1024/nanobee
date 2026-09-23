"""MessageTool 单元测试 — 附件投递契约。

覆盖场景：
1. 参数 schema：只声明 media；media 必填且至少一项（声明即契约）
2. execute：附件路径校验与如实回执（不承诺投递、不复述正文）
3. collect_message_tool_media：只收集附件（正文不经过本工具）
4. 不变量一：正文唯一信道 = 最终回复（_assemble_outbound）
5. 不变量二：附件收集窗口 = 本轮新增消息，历史不参与（_turn_increment）
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobee.agent.loop import AgentLoop
from nanobee.agent.messages import InboundMessage
from nanobee.agent.tools.message import MessageTool, collect_message_tool_media


def _bare_loop() -> AgentLoop:
    """不跑完整初始化的 AgentLoop：_assemble_outbound 不依赖实例状态。"""
    return AgentLoop.__new__(AgentLoop)


def _msg() -> InboundMessage:
    """构造最小入站消息。"""
    return InboundMessage(channel="channel_cli", sender_id="u1", chat_id="c1", content="发周报")


class TestMessageToolSchema:
    """MessageTool 参数 schema 测试。"""

    def test_tool_name(self) -> None:
        """工具名称应为 'message'。"""
        tool = MessageTool()
        assert tool.name == "message"

    def test_description_states_attachment_only_contract(self) -> None:
        """描述必须明确正文不走本工具（契约的诚实性载体）。"""
        tool = MessageTool()
        description = tool.description
        assert description
        assert "ATTACHMENTS ONLY" in description
        assert "final reply" in description.lower()

    def test_schema_declares_only_media(self) -> None:
        """声明即契约：不声明没有语义的参数。"""
        tool = MessageTool()
        params = tool.parameters
        assert params["type"] == "object"
        assert set(params["properties"]) == {"media"}
        assert params["required"] == ["media"]
        assert params["properties"]["media"]["minItems"] == 1


class TestMessageToolParamValidation:
    """参数校验测试（非法调用执行不到 execute）。"""

    def test_valid_media_accepted(self) -> None:
        """合法附件列表通过校验。"""
        tool = MessageTool()
        assert tool.validate_params({"media": ["/data/report.xlsx"]}) == []

    def test_content_only_call_rejected(self) -> None:
        """事故形态：把正文塞进 content 且无附件 → 校验拒绝。"""
        tool = MessageTool()
        errors = tool.validate_params({"content": "| a | b |"})
        assert errors
        assert any("media" in e for e in errors)

    def test_empty_media_rejected(self) -> None:
        """空附件列表被 minItems 拒绝。"""
        tool = MessageTool()
        assert tool.validate_params({"media": []})

    def test_missing_media_rejected(self) -> None:
        """无参数调用被 required 拒绝。"""
        tool = MessageTool()
        assert tool.validate_params({})


class TestMessageToolExecute:
    """MessageTool.execute() 测试。"""

    @pytest.mark.asyncio
    async def test_execute_with_valid_media(self, tmp_path: Path) -> None:
        """有效本地文件路径 → 如实回执。"""
        report = tmp_path / "report.pdf"
        report.write_text("report content", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(media=[str(report)])
        assert "已登记 1 个附件" in result
        assert "report.pdf" in result

    @pytest.mark.asyncio
    async def test_execute_with_http_media(self) -> None:
        """HTTP/HTTPS URL 不校验存在性，直接通过。"""
        tool = MessageTool()
        result = await tool.execute(media=["https://example.com/img.png"])
        assert "已登记 1 个附件" in result
        assert "img.png" in result

    @pytest.mark.asyncio
    async def test_execute_with_invalid_media_path(self) -> None:
        """不存在的本地文件路径应报错。"""
        tool = MessageTool()
        result = await tool.execute(media=["/nonexistent/path/file.pdf"])
        assert "错误" in result
        assert "不存在" in result
        assert "file.pdf" in result

    @pytest.mark.asyncio
    async def test_execute_with_mixed_media(self, tmp_path: Path) -> None:
        """混合有效和无效路径时，仅报错无效路径。"""
        valid = tmp_path / "valid.txt"
        valid.write_text("data", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(media=[str(valid), "/bad/path.pdf"])
        assert "错误" in result
        assert "bad/path.pdf" in result
        # 有效路径不应出现在错误消息中
        assert "valid.txt" not in result.split("错误")[1]

    @pytest.mark.asyncio
    async def test_execute_with_non_string_media(self) -> None:
        """非字符串类型的 media 元素应报错。"""
        tool = MessageTool()
        result = await tool.execute(media=[123, True])
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_execute_multiple_valid_media(self, tmp_path: Path) -> None:
        """多个有效本地文件路径。"""
        f1 = tmp_path / "f1.pdf"
        f2 = tmp_path / "f2.png"
        f1.write_text("c1", encoding="utf-8")
        f2.write_text("c2", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(media=[str(f1), str(f2)])
        assert "已登记 2 个附件" in result
        assert "f1.pdf" in result
        assert "f2.png" in result

    @pytest.mark.asyncio
    async def test_execute_relative_path_without_absolute_file(self) -> None:
        """相对路径不触发存在性检查（既有行为）。"""
        tool = MessageTool()
        result = await tool.execute(media=["relative/file.txt"])
        assert "错误" not in result

    @pytest.mark.asyncio
    async def test_receipt_does_not_promise_delivery(self, tmp_path: Path) -> None:
        """回执不得承诺工具无法感知的动作（如「已加入发送队列」/「随回复投递」）。"""
        report = tmp_path / "report.pdf"
        report.write_text("x", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(media=[str(report)])
        assert "发送队列" not in result
        assert "将在本轮结束时" not in result
        # 回执必须把契约说清：正文不走本工具
        assert "写在最终回复里" in result

    @pytest.mark.asyncio
    async def test_legacy_content_kwarg_is_ignored_and_not_echoed(self, tmp_path: Path) -> None:
        """旧形态残留：content 参数被忽略，且不在回执中复述。"""
        report = tmp_path / "report.pdf"
        report.write_text("x", encoding="utf-8")

        tool = MessageTool()
        result = await tool.execute(media=[str(report)], content="| 表格 |")
        assert "表格" not in result
        assert "report.pdf" in result


class TestCollectMessageToolMedia:
    """collect_message_tool_media() 测试 —— 只收集附件。"""

    def test_collect_from_single_tool_call(self) -> None:
        """从单个 tool_call 中提取附件。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"media": ["/path/to/report.pdf"]}),
                        },
                    }
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/path/to/report.pdf"]

    def test_collect_from_multiple_tool_calls_keeps_declaration_order(self) -> None:
        """多个 tool_call 按声明顺序汇总。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/path/f1.pdf"]})}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/path/f2.pdf"]})}},
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/path/f1.pdf", "/path/f2.pdf"]

    def test_collect_dedup_media(self) -> None:
        """重复的 media 路径自动去重（首次出现位置优先）。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/same/file.pdf"]})}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"media": ["/same/file.pdf", "/other/file.png"]}),
                        },
                    }
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/same/file.pdf", "/other/file.png"]

    def test_collect_ignores_legacy_content_argument(self) -> None:
        """旧形态历史里的 content 参数天然惰性，不参与任何收集。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"content": "| 表格 |", "media": ["/a.pdf"]}),
                        },
                    }
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/a.pdf"]

    def test_collect_ignores_non_message_tool_calls(self) -> None:
        """忽略非 message 的 tool_call。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp"})}},
                ],
            },
        ]
        assert collect_message_tool_media(messages) == []

    def test_collect_no_message_calls(self) -> None:
        """无 message tool_call 时返回空列表。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        assert collect_message_tool_media(messages) == []

    def test_collect_empty_messages(self) -> None:
        """空消息列表返回空列表。"""
        assert collect_message_tool_media([]) == []

    def test_collect_invalid_json_arguments(self) -> None:
        """JSON 解析失败的 tool_call 被优雅跳过。"""
        messages = [
            {"role": "assistant", "tool_calls": [{"function": {"name": "message", "arguments": "not-json"}}]},
        ]
        assert collect_message_tool_media(messages) == []

    def test_collect_non_dict_message(self) -> None:
        """非 dict 类型的消息被跳过。"""
        messages = [
            "not a dict",
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/a.pdf"]})}},
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/a.pdf"]

    def test_collect_tool_calls_with_non_dict_item(self) -> None:
        """tool_calls 列表中的非 dict 元素被跳过。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    "not a dict",
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/a.pdf"]})}},
                ],
            },
        ]
        assert collect_message_tool_media(messages) == ["/a.pdf"]

    def test_collect_scalar_tool_calls_is_skipped(self) -> None:
        """tool_calls 为标量（脏数据）时不抛异常。"""
        messages = [{"role": "assistant", "tool_calls": 5}]
        assert collect_message_tool_media(messages) == []

    @pytest.mark.parametrize("dirty", [None, 5, "not-a-list"])
    def test_collect_non_list_media_is_skipped(self, dirty: object) -> None:
        """media 非列表（null / 标量 / 字符串）时跳过该次调用，不抛异常。"""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": dirty})}},
                ],
            },
        ]
        assert collect_message_tool_media(messages) == []


class TestOutboundContentInvariant:
    """正文唯一信道不变量：OutboundMessage.content 只来自最终回复。"""

    def test_content_comes_only_from_final_reply(self) -> None:
        """message 工具调用里的 content 参数不进入出站正文。"""
        turn_messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"content": "| 表格 |", "media": ["/data/report.xlsx"]}),
                        },
                    }
                ],
            },
            {"role": "tool", "content": "已登记 1 个附件：report.xlsx"},
            {"role": "assistant", "content": "周报已生成，见附件。"},
        ]

        outbound = _bare_loop()._assemble_outbound(
            _msg(), "周报已生成，见附件。", turn_messages, "completed", False,
        )

        assert outbound is not None
        assert outbound.content == "周报已生成，见附件。"
        assert "表格" not in outbound.content
        assert outbound.media == ["/data/report.xlsx"]

    def test_pure_attachment_turn_keeps_media(self) -> None:
        """纯附件轮：正文为空时附件仍随占位正文投递。"""
        turn_messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "message", "arguments": json.dumps({"media": ["/data/report.xlsx"]})}},
                ],
            },
        ]

        outbound = _bare_loop()._assemble_outbound(_msg(), None, turn_messages, "completed", False)

        assert outbound is not None
        assert outbound.media == ["/data/report.xlsx"]
        assert outbound.content


class TestAttachmentWindowInvariant:
    """附件收集窗口不变量：只认本轮新增消息，历史一概不参与。"""

    @staticmethod
    def _ctx(initial: list[dict], all_messages: list[dict]) -> SimpleNamespace:
        return SimpleNamespace(
            initial_messages=initial,
            all_messages=all_messages,
            context_id="cli:default",
        )

    def test_increment_excludes_history(self) -> None:
        """本轮增量 = 锚点之后的部分。"""
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        increment = [
            {"role": "assistant", "content": "好的"},
        ]

        assert AgentLoop._turn_increment(self._ctx(history, history + increment)) == increment

    @pytest.mark.parametrize(
        ("initial", "all_messages"),
        [
            ([{"role": "user", "content": "hi"}], []),  # 消息被裁短
            ([], [{"role": "assistant", "content": "x"}]),  # 无锚点
        ],
    )
    def test_increment_empty_when_boundary_broken(
        self, initial: list[dict], all_messages: list[dict],
    ) -> None:
        """切片前提不满足时返回空列表（宁可少收，不可重投）。"""
        assert AgentLoop._turn_increment(self._ctx(initial, all_messages)) == []

    def test_history_attachment_is_not_redelivered(self) -> None:
        """历史里声明过的附件不会随之后的每一轮再次投递。"""
        old_call = {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "message",
                        "arguments": json.dumps({"media": ["/data/old-weekly.xlsx"]}),
                    },
                }
            ],
        }
        history = [
            {"role": "user", "content": "把上周周报发我"},
            old_call,
            {"role": "tool", "content": "已登记 1 个附件：old-weekly.xlsx"},
            {"role": "assistant", "content": "已发送。"},
        ]
        increment = [{"role": "assistant", "content": "本周没有附件。"}]
        ctx = self._ctx(history, history + increment)

        outbound = _bare_loop()._assemble_outbound(
            _msg(), "本周没有附件。", AgentLoop._turn_increment(ctx), "completed", False,
        )

        assert outbound is not None
        assert outbound.media == []

    def test_current_turn_attachment_is_collected(self) -> None:
        """本轮声明的附件正常收集（窗口收窄不误伤本轮）。"""
        history = [{"role": "user", "content": "把上周周报发我"}]
        increment = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "message",
                            "arguments": json.dumps({"media": ["/data/new-weekly.xlsx"]}),
                        },
                    }
                ],
            },
            {"role": "tool", "content": "已登记 1 个附件：new-weekly.xlsx"},
            {"role": "assistant", "content": "本周周报见附件。"},
        ]
        ctx = self._ctx(history, history + increment)

        outbound = _bare_loop()._assemble_outbound(
            _msg(), "本周周报见附件。", AgentLoop._turn_increment(ctx), "completed", False,
        )

        assert outbound is not None
        assert outbound.media == ["/data/new-weekly.xlsx"]
