"""Echo 工具插件（测试用）"""

from __future__ import annotations

import logging
from typing import Any

from nanobee.plugins.tool import ToolPlugin

logger = logging.getLogger(__name__)


class EchoToolPlugin(ToolPlugin):
    """回显工具"""

    name = "tool-echo"
    version = "1.0.0"

    def get_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显输入的内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要回显的文本",
                        }
                    },
                    "required": ["text"],
                },
            }
        }]

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        if tool_name == "echo":
            return f"[echo] {kwargs.get('text', '')}"
        raise ValueError(f"未知工具: {tool_name}")
