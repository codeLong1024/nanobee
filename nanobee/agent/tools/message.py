"""Message tool — 附件投递工具。

LLM 通过本工具结构化声明要投递的**附件**；实际投递由
``AgentLoop._assemble_outbound()`` 在轮次结束时扫描 ``message`` 工具调用，
把附件路径合并进出站消息后交给通道发送。

契约（本工具的唯一语义）：
- 只投递附件。**要送达的正文必须写在最终回复里**，本工具不承载正文。
- 无附件的调用没有意义，故 ``media`` 必填且至少一项（声明层拒绝）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobee.agent.tools.base import Tool, tool_parameters


@tool_parameters({
    "type": "object",
    "properties": {
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "List of local file paths (absolute) or URLs to attach. "
                "Example: [\"/path/to/report.pdf\", \"/path/to/image.png\"]"
            ),
        },
    },
    "required": ["media"],
})
class MessageTool(Tool):
    """向用户投递附件（唯一语义）。

    本工具是结构化标记：实际发送由 ``AgentLoop._assemble_outbound()``
    在轮次结束时扫描对话历史里的 ``message`` 工具调用，收集 ``media``
    路径后合并进最终 ``OutboundMessage``。

    正文不通过本工具投递——要送达的文字必须写在最终回复里。
    """

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Attach files to the reply that will be sent to the user.\n\n"
            "Use this when you need to:\n"
            "- Send a file/attachment to the user (e.g. generated report, image, document)\n\n"
            "Put the file's absolute path in the media list, and the system will\n"
            "automatically upload and send it as a native file message.\n\n"
            "This tool delivers ATTACHMENTS ONLY: any text the user should read MUST be\n"
            "written in your final reply text — text passed to this tool is not delivered."
        )

    async def execute(self, **kwargs: Any) -> str:
        """校验附件路径，返回如实回执。

        参数形状（``media`` 必填、至少一项）已在调用前的
        ``ToolRegistry.prepare_call`` 校验完成，非法调用到不了这里，
        故本方法不再重复守卫。

        Returns:
            供 LLM 阅读的回执：只陈述已经发生的事实（登记了哪几个附件），
            不承诺工具无法感知的投递结果。
        """
        media = kwargs.get("media") or []

        # 校验 media 路径：本地文件必须存在，避免 LLM 以为发送成功但实际传了无效路径
        invalid_paths: list[str] = []
        valid_media: list[str] = []
        for p in media:
            if not isinstance(p, str):
                invalid_paths.append(repr(p))
                continue
            # HTTP/HTTPS URL 不校验存在性（需要在发送阶段验证）
            if p.startswith(("http://", "https://")):
                valid_media.append(p)
                continue
            path = Path(p)
            if path.is_absolute() and not path.exists():
                invalid_paths.append(p)
                continue
            valid_media.append(p)

        if invalid_paths:
            return (
                f"错误：以下 media 文件路径不存在或无效：{', '.join(invalid_paths)}。"
                f"请检查文件是否已正确生成，并传入有效的绝对路径。"
            )

        # 回执只陈述已发生的事实（登记了哪几个附件），不承诺尚未发生的投递
        filename_hints: list[str] = []
        for path in valid_media:
            try:
                filename_hints.append(Path(path).name)
            except Exception:
                filename_hints.append(path)

        file_list = ", ".join(filename_hints)
        return (
            f"已登记 {len(valid_media)} 个附件：{file_list}。"
            f"（正文不通过本工具投递，请把要送达的文字写在最终回复里。）"
        )


def collect_message_tool_media(
    turn_messages: list[dict[str, Any]],
) -> list[str]:
    """从**本轮**消息中收集 ``message`` 工具调用声明的附件路径。

    扫描 ``turn_messages`` 中所有 ``message`` 工具调用，按声明顺序汇总
    ``media`` 路径并去重。本工具只承载附件，因此不提取任何正文——正文的
    唯一信道是最终回复（``final_content``）。

    只处理本轮消息：历史里的 ``message`` 工具调用属于已完成的投递，重复
    扫描会把它重新塞进之后每一轮的出站消息。

    Args:
        turn_messages: 本轮新增消息（不含历史）

    Returns:
        去重后的附件路径列表（保持首次声明的顺序）
    """
    media_paths: list[str] = []
    seen_media: set[str] = set()

    for msg in turn_messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not isinstance(func, dict) or func.get("name") != "message":
                continue
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            # 模型可能给出非列表的 media（如 null / 字符串）：不可迭代或会逐字符
            # 产出垃圾路径，直接跳过该次调用（参数合法性由 prepare_call 前置校验）
            declared = args.get("media")
            if not isinstance(declared, list):
                continue
            for p in declared:
                if isinstance(p, str) and p not in seen_media:
                    media_paths.append(p)
                    seen_media.add(p)

    return media_paths


__all__ = ["MessageTool", "collect_message_tool_media"]
