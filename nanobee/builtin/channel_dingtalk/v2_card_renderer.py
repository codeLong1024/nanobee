"""钉钉 v2 结构化进度卡渲染器 — agent 运行事件 → cardParamMap 渲染快照。

对齐官方连接器（dingtalk-openclaw-connector）``StructuredCardDraft`` 的契约：
v2 结构化模板（blockList）下，工具步骤是 ``type:2`` block、正文 ``type:0``、
状态行 ``statusLine``，正文流式走 ``content`` 变量（打字机）。

协议约束（官方 ``writeV2Card`` + 第一轮真机 demo 实证）：
- 变量全部为字符串值：blockList（JSON 串）、content、copy_content、
  flowStatus（"1" 初始/"2" 生成中/"3" 已完成）、hasAction、statusLine
- 非终帧正文只进 ``content``（打字机通道），不进 blockList（防双渲染）；
  终帧 blockList 含步骤 + 正文块，``copy_content`` 落完整正文
- 单 block ≤6000 字节（UTF-8 计数，留余量）；工具步骤上限 32 条
- 终态后拒绝迟到帧

本模块为纯逻辑（无 IO）：每帧产出 ``(streaming_content, card_param_map)``
渲染快照，HTTP 由 ``v2_card_client`` 执行。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

# 官方公共 v2 模板（当前应用实证可直接使用；如无权限需导入官方仓库
# docs/assets/card-template-v2.json 后由配置指定自有模板）
V2_CARD_TEMPLATE_ID = "675cde2f-f526-40cb-b828-f5b2b57b8b77.schema"

# flowStatus 取值：1=初始（创建）/ 2=生成中 / 3=已完成
FLOW_INIT = "1"
FLOW_INPUTING = "2"
FLOW_FINISHED = "3"

# 单 block 字节上限（官方实现按 UTF-8 计数，留 256 字节余量）
BLOCK_BYTES = 6000
# 切分安全阈值：留出的余量同时覆盖 block 结构 JSON 包装的开销
_SAFE_BLOCK_BYTES = BLOCK_BYTES - 256

# 工具步骤数量上限（官方连接器同值：防长任务耗尽卡片变量空间）
MAX_STEPS = 32

# 步骤状态文案（与 progress_events 的 phase 对应）
_STATE_LABELS = {"running": "执行中", "completed": "已完成", "failed": "失败"}


def _hard_split_line(line: str, limit: int) -> list[str]:
    """单行超限时按字节上限硬切（码点安全，不切断多字节字符）。

    Args:
        line: 超限的单行文本。
        limit: 每个切片的最大字节数。

    Returns:
        切片列表，各片字节均 ≤ limit，按序拼接可复原原行。
    """
    pieces: list[str] = []
    buf: list[str] = []
    buf_bytes = 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if buf_bytes + width > limit and buf:
            pieces.append("".join(buf))
            buf, buf_bytes = [], 0
        buf.append(ch)
        buf_bytes += width
    if buf:
        pieces.append("".join(buf))
    return pieces


def split_markdown(text: str) -> list[str]:
    """按单 block 字节上限切分 markdown，尽量在行边界断开。

    单行本身超限时退化为字节级硬切（对齐官方实现「按行/字符切分」语义），
    保证任何切片都不超限。

    Args:
        text: 待切分文本。

    Returns:
        切分后的文本片段列表。
    """
    chunks: list[str] = []
    current = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if len(line.encode("utf-8")) > _SAFE_BLOCK_BYTES:
            # 单行超限（长 URL/base64/单行 JSON）：先落既有累积，再硬切该行
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split_line(line, _SAFE_BLOCK_BYTES))
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) > _SAFE_BLOCK_BYTES and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


@dataclass
class V2CardFrame:
    """一帧渲染快照：client 据此执行一次「streaming + instances」两步更新。

    Attributes:
        streaming_content: PUT /card/streaming 的 content 值（终帧为空串，
            用于关闭流式生命周期）。
        card_param_map: PUT /card/instances 的 cardData.cardParamMap。
        finished: 是否为终态帧。
    """

    streaming_content: str
    card_param_map: dict[str, str]
    finished: bool


class V2CardDraft:
    """v2 进度卡草稿：维护工具步骤与流式正文，逐帧产出渲染快照。

    与 agent 运行事件一一对应::

        draft = V2CardDraft(agent_name="nanobee", model="...")
        client.create(draft.create_param_map(), ...)        # 创建卡片
        for event in agent_events:
            if event.phase == "start":
                draft.tool_started(call_id, name)
            elif event.phase in ("end", "error"):
                draft.tool_finished(call_id, name, error=...)
            else:
                draft.set_answer(accumulated_text)
            client.update(draft.frame())                    # 两步式推送
        client.finish(draft.frame(finished=True))           # 终态

    非线程安全：同一张卡片的帧构造须串行（由 client 的写入串行化保证）。
    """

    def __init__(
        self,
        agent_name: str = "nanobee",
        model: str = "",
        *,
        show_metadata: bool = True,
    ) -> None:
        """初始化草稿。

        Args:
            agent_name: 状态行展示的 agent 名。
            model: 状态行展示的模型名（为空则不展示）。
            show_metadata: 是否在卡片上展示状态行。
        """
        self._agent_name = agent_name
        self._model = model
        self._show_metadata = show_metadata
        self._started_at = time.time()
        # 有序步骤列表：[(call_id, 工具名, 状态键)]
        self._steps: list[tuple[str, str, str]] = []
        # 正文分段缓冲：流式增量按段追加，避免不可变字符串的 O(n²) 累积拷贝
        self._answer_parts: list[str] = []
        self._finished = False
        self._final_error: str | None = None

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    def tool_started(self, call_id: str, name: str) -> None:
        """工具开始：追加或刷新步骤行（执行中）。

        Args:
            call_id: 工具调用 ID（进度事件的 call_id）。
            name: 工具名。
        """
        if self._finished:
            return
        # 同 call_id 重复 start：仅刷新，不追加新行
        self._steps = [s for s in self._steps if s[0] != call_id]
        self._steps.append((call_id, name, "running"))
        if len(self._steps) > MAX_STEPS:
            self._steps = self._steps[-MAX_STEPS:]

    def tool_finished(self, call_id: str, name: str, *, error: str | None = None) -> None:
        """工具结束：原地更新步骤状态（已完成/失败）。

        Args:
            call_id: 工具调用 ID。
            name: 工具名。
            error: 失败详情；None 表示成功。
        """
        if self._finished:
            return
        state = "failed" if error else "completed"
        for idx, (cid, _, _) in enumerate(self._steps):
            if cid == call_id:
                self._steps[idx] = (cid, name, state)
                return
        # 未发过 start 的结束事件（兜底）：追加终态步骤
        self._steps.append((call_id, name, state))
        if len(self._steps) > MAX_STEPS:
            self._steps = self._steps[-MAX_STEPS:]

    def set_answer(self, text: str) -> None:
        """覆盖流式正文（整值设置）。

        Args:
            text: 正文的完整值。
        """
        if self._finished:
            return
        self._answer_parts = [text]

    def append_answer(self, delta: str) -> None:
        """追加流式正文增量（O(1) 入缓冲，出帧时一次性拼接）。

        Args:
            delta: 本帧新增文本。
        """
        if self._finished or not delta:
            return
        self._answer_parts.append(delta)

    def finalize(self, *, error: str | None = None) -> None:
        """进入终态（此后拒绝一切迟到事件）。"""
        self._finished = True
        self._final_error = error

    # ------------------------------------------------------------------
    # 渲染快照
    # ------------------------------------------------------------------

    def create_param_map(self) -> dict[str, str]:
        """创建卡片的初始 cardParamMap（flowStatus="1"）。

        Returns:
            官方 create 契约要求的初始变量集。
        """
        return {
            "blockList": "[]",
            "content": "",
            "copy_content": "",
            "statusLine": "",
            "flowStatus": FLOW_INIT,
            "hasAction": "false",
        }

    def frame(self, *, finished: bool = False) -> V2CardFrame:
        """产出当前帧的渲染快照。

        Args:
            finished: 是否为终态帧（终帧正文进 blockList、copy_content 落
                完整正文、flowStatus 置已完成）。

        Returns:
            V2CardFrame：streaming 值 + instances 的 cardParamMap。
        """
        if finished:
            self._finished = True
        answer = self.answer
        blocks: list[dict[str, Any]] = [
            {"type": 2, "markdown": self._step_text(name, state)}
            for _, name, state in self._steps
        ]
        if finished and answer.strip():
            blocks.extend(
                {"type": 0, "markdown": chunk}
                for chunk in split_markdown(answer.strip())
            )
        outcome = "执行失败" if self._final_error else "已完成" if finished else "生成中"
        param_map: dict[str, str] = {
            "blockList": json.dumps(blocks, ensure_ascii=False),
            "content": answer,
            "copy_content": answer if finished else "",
            "flowStatus": FLOW_FINISHED if finished else FLOW_INPUTING,
            "hasAction": "false",
        }
        status_line = self._status_line(outcome)
        if status_line is not None:
            param_map["statusLine"] = status_line
        return V2CardFrame(
            streaming_content="" if finished else answer,
            card_param_map=param_map,
            finished=finished,
        )

    @property
    def finished(self) -> bool:
        """卡片是否已终态（终态后拒绝迟到事件）。"""
        return self._finished

    @property
    def answer(self) -> str:
        """当前累积正文（供会话管理器追加/覆盖前读取）。"""
        return "".join(self._answer_parts)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _step_text(self, name: str, state: str) -> str:
        """构造步骤行文本。"""
        return f"{name} · {_STATE_LABELS.get(state, state)}"

    def _status_line(self, outcome: str) -> str | None:
        """构造状态行："agent · model · 工具 N · 耗时 · 状态"。

        Returns:
            状态行文本；show_metadata=False 时返回 None（变量缺省即隐藏）。
        """
        if not self._show_metadata:
            return None
        elapsed = max(0, int(time.time() - self._started_at))
        parts = [self._agent_name, self._model, f"工具 {len(self._steps)}", f"{elapsed}s", outcome]
        return " · ".join(part for part in parts if part)


__all__ = ["V2_CARD_TEMPLATE_ID", "V2CardDraft", "V2CardFrame", "split_markdown"]
