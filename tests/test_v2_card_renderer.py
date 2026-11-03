"""v2 结构化进度卡渲染器测试。

协议契约参照：官方连接器 StructuredCardDraft/writeV2Card +
第一轮真机 demo（examples/dingtalk_card_v2_demo.py）。

覆盖场景：
1. create_param_map 的初始变量契约（flowStatus="1"、blockList="[]"）
2. frame 两步更新契约：streaming_content 承载打字机、cardParamMap 全字符串
3. 渲染分工：非终帧正文只走 content 不进 blockList；终帧 blockList 含步骤+正文
4. 步骤生命周期：追加执行中 → 原地已完成/失败；重复 start 不堆行
5. 步骤上限 MAX_STEPS 与字节截断 split_markdown
6. 终态拒绝迟到帧；状态行 show_metadata 开关
"""

from __future__ import annotations

import json

from nanobee.builtin.channel_dingtalk.v2_card_renderer import (
    FLOW_FINISHED,
    FLOW_INIT,
    FLOW_INPUTING,
    MAX_STEPS,
    V2CardDraft,
    split_markdown,
)


def _blocks(frame_json: str) -> list[dict]:
    """解析 blockList 变量。"""
    return json.loads(frame_json)


class TestCreateParamMap:
    """创建契约。"""

    def test_initial_values(self) -> None:
        """初始 cardParamMap 与官方 create 契约逐字对齐。"""
        param_map = V2CardDraft().create_param_map()
        assert param_map == {
            "blockList": "[]",
            "content": "",
            "copy_content": "",
            "statusLine": "",
            "flowStatus": FLOW_INIT,
            "hasAction": "false",
        }

    def test_all_values_are_strings(self) -> None:
        """全部变量为字符串（cardParamMap 契约）。"""
        assert all(isinstance(v, str) for v in V2CardDraft().create_param_map().values())


class TestFrame:
    """帧快照契约。"""

    def test_running_frame(self) -> None:
        """生成中帧：flowStatus="2"、streaming=正文、copy_content 为空。"""
        draft = V2CardDraft(show_metadata=False)
        draft.set_answer("第一段")
        frame = draft.frame()
        assert frame.streaming_content == "第一段"
        assert frame.card_param_map["flowStatus"] == FLOW_INPUTING
        assert frame.card_param_map["content"] == "第一段"
        assert frame.card_param_map["copy_content"] == ""
        assert not frame.finished

    def test_non_final_answer_not_in_blocks(self) -> None:
        """非终帧：正文不进 blockList（防双渲染）。"""
        draft = V2CardDraft()
        draft.tool_started("c1", "read_file")
        draft.set_answer("正文")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert [b["type"] for b in blocks] == [2]
        assert blocks[0]["markdown"] == "read_file · 执行中"

    def test_final_frame_contract(self) -> None:
        """终帧：blockList 含步骤+正文块、copy_content 落正文、flowStatus="3"。"""
        draft = V2CardDraft()
        draft.tool_started("c1", "read_file")
        draft.tool_finished("c1", "read_file")
        draft.set_answer("最终正文")
        frame = draft.frame(finished=True)
        assert frame.streaming_content == ""
        assert frame.card_param_map["flowStatus"] == FLOW_FINISHED
        assert frame.card_param_map["copy_content"] == "最终正文"
        blocks = _blocks(frame.card_param_map["blockList"])
        assert [b["type"] for b in blocks] == [2, 0]
        assert blocks[0]["markdown"] == "read_file · 已完成"
        assert blocks[1]["markdown"] == "最终正文"

    def test_status_line_present_by_default(self) -> None:
        """默认展示状态行：agent · 工具数 · 耗时 · 状态。"""
        draft = V2CardDraft(agent_name="nanobee", model="m1")
        frame = draft.frame()
        status_line = frame.card_param_map["statusLine"]
        assert status_line.startswith("nanobee · m1 · ")
        assert status_line.endswith("· 生成中")

    def test_status_line_hidden_when_disabled(self) -> None:
        """show_metadata=False：cardParamMap 不含 statusLine 键。"""
        frame = V2CardDraft(show_metadata=False).frame()
        assert "statusLine" not in frame.card_param_map


class TestToolSteps:
    """步骤生命周期。"""

    def test_lifecycle_states(self) -> None:
        """执行中 → 已完成；error → 失败。"""
        draft = V2CardDraft(show_metadata=False)
        draft.tool_started("c1", "read_file")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert blocks[0]["markdown"] == "read_file · 执行中"

        draft.tool_finished("c1", "read_file")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert blocks[0]["markdown"] == "read_file · 已完成"

        draft.tool_started("c2", "boom_tool")
        draft.tool_finished("c2", "boom_tool", error="boom")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert blocks[1]["markdown"] == "boom_tool · 失败"

    def test_duplicate_start_no_duplicate_step(self) -> None:
        """同 call_id 重复 start：只保留一条步骤。"""
        draft = V2CardDraft(show_metadata=False)
        draft.tool_started("c1", "read_file")
        draft.tool_started("c1", "read_file")
        assert len(_blocks(draft.frame().card_param_map["blockList"])) == 1

    def test_finish_without_start_creates_step(self) -> None:
        """未发过 start 的结束事件：兜底追加终态步骤。"""
        draft = V2CardDraft(show_metadata=False)
        draft.tool_finished("cx", "web_search")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert blocks[0]["markdown"] == "web_search · 已完成"

    def test_max_steps_eviction(self) -> None:
        """超过 MAX_STEPS：最早的步骤被移除。"""
        draft = V2CardDraft(show_metadata=False)
        for idx in range(MAX_STEPS + 3):
            draft.tool_started(f"c{idx}", f"tool_{idx}")
        blocks = _blocks(draft.frame().card_param_map["blockList"])
        assert len(blocks) == MAX_STEPS
        assert all("tool_0 " not in b["markdown"] for b in blocks[:1])


class TestTerminal:
    """终态语义。"""

    def test_finalize_rejects_late_events(self) -> None:
        """终态后：工具/正文事件被忽略，终帧仍可产出。"""
        draft = V2CardDraft(show_metadata=False)
        draft.set_answer("正文")
        draft.finalize()
        draft.tool_started("c1", "late_tool")
        draft.set_answer("迟到正文")
        frame = draft.frame(finished=True)
        blocks = _blocks(frame.card_param_map["blockList"])
        assert blocks == [{"type": 0, "markdown": "正文"}]
        assert draft.finished is True

    def test_finalize_error_outcome(self) -> None:
        """失败终态：状态行为执行失败。"""
        draft = V2CardDraft(agent_name="nanobee")
        draft.finalize(error="boom")
        frame = draft.frame(finished=True)
        assert frame.card_param_map["statusLine"].endswith("· 执行失败")


class TestSplitMarkdown:
    """字节截断。"""

    def test_short_text_single_chunk(self) -> None:
        """短文本不切分。"""
        assert split_markdown("短文本") == ["短文本"]

    def test_long_text_split_on_lines(self) -> None:
        """超长文本在行边界切分，单块不超上限。"""
        line = "a" * 200 + "\n"
        chunks = split_markdown(line * 100)
        assert len(chunks) > 1
        assert all(len(c.encode("utf-8")) <= 6000 for c in chunks)

    def test_oversized_single_line_hard_split(self) -> None:
        """防复发（评审 #2）：单行超限必须被硬切，任何 chunk 不超上限。

        长URL/base64/单行 JSON 是现实输入；旧实现在行边界切分下
        首个超长行整块保留，突破单 block ≤6000 字节契约。
        """
        text = "x" * 8000 + "\n结尾"  # 超长行位于首行（current 为空路径）
        chunks = split_markdown(text)
        assert all(len(c.encode("utf-8")) <= 6000 for c in chunks)
        # 内容无丢失：去掉切分引入的换行后可复原
        assert "".join(chunks).replace("\n", "", 1) == text.replace("\n", "", 1) or \
            sum(c.count("x") for c in chunks) == 8000

    def test_oversized_cjk_line_no_char_loss(self) -> None:
        """多字节字符行硬切：不切断字符、不丢字符。"""
        text = "汉" * 4000  # 12000 字节，必然超限
        chunks = split_markdown(text)
        assert all(len(c.encode("utf-8")) <= 6000 for c in chunks)
        assert "".join(chunks) == text

    def test_line_exactly_at_limit_kept_whole(self) -> None:
        """恰好等于安全上限的行不触发切分。"""
        line = "a" * (6000 - 256)
        assert split_markdown(line) == [line]

    def test_oversized_line_mid_text(self) -> None:
        """超长行夹在正常行中间：前后文各自成块，超长行被硬切。"""
        text = "开头\n" + "y" * 8000 + "\n收尾"
        chunks = split_markdown(text)
        assert all(len(c.encode("utf-8")) <= 6000 for c in chunks)
        assert chunks[0] == "开头"
        assert chunks[-1] == "收尾"
        assert sum(c.count("y") for c in chunks) == 8000
