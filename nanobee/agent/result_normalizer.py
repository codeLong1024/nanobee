"""工具结果标准化——确保非空、可序列化、预算截断、可选持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobee.utils.helpers import maybe_persist_tool_result, truncate_text
from nanobee.utils.logger import logger
from nanobee.utils.runtime import ensure_nonempty_tool_result


class ResultNormalizer:
    """将原始工具结果标准化为适合 LLM 消费的格式。

    标准化流水线：
    1. 确保非空 — 插入占位文本
    2. 序列化 — 非字符串结果转 JSON
    3. 持久化 — 超长结果写入文件，返回引用
    4. 截断 — 超过 max_chars 的结果做尾部截断
    """

    @staticmethod
    def normalize(
        result: Any,
        *,
        tool_name: str,
        tool_call_id: str,
        workspace: Path | None,
        context_id: str | None,
        max_chars: int,
    ) -> str:
        """标准化单个工具结果。

        Args:
            result: 原始工具执行结果。
            tool_name: 工具名称（用于非空占位）。
            tool_call_id: 工具调用 ID（用于持久化文件命名）。
            workspace: 工作区路径（用于持久化文件存放）。
            context_id: 会话标识（用于持久化目录）。
            max_chars: 结果最大字符数（超出后截断或持久化）。

        Returns:
            标准化后的字符串结果。
        """
        result = ensure_nonempty_tool_result(tool_name, result)

        # 非字符串结果（如工具返回的 dict）转为 JSON 字符串，防止 LLM 后端
        # 将其解析为缺失 type 字段的多模态内容导致 400 错误。
        if not isinstance(result, str) and result is not None:
            try:
                result = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                result = str(result)

        try:
            content = maybe_persist_tool_result(
                workspace,
                context_id,
                tool_call_id,
                result,
                max_chars=max_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                context_id or "default",
            )
            content = result

        if isinstance(content, str) and len(content) > max_chars:
            return truncate_text(content, max_chars)
        return content
