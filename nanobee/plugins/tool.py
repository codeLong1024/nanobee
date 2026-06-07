"""ToolPlugin 接口 - 工具插件"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from nanobee.plugins.base import NanobeePlugin

from nanobee.utils.logger import logger



class ToolPlugin(NanobeePlugin):
    """工具插件基类

    每个工具插件可以提供一个或多个工具给 Agent 使用。

    沙箱通过 ContextVar 注入（见 nanobee/kernel/context_sandbox_var.py），
    插件内部使用 current_sandbox() 获取当前请求的沙箱实例。
    """

    plugin_type = "tool"

    @abstractmethod
    def get_tools(self) -> list[dict[str, Any]]:
        """获取工具定义列表（OpenAI function schema 格式）

        Returns:
            工具定义列表，每个元素包含 name, description, parameters
        """
        ...

    @abstractmethod
    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行指定工具

        Args:
            tool_name: 工具名称
            **kwargs: 工具参数

        Returns:
            工具执行结果

        Raises:
            ValueError: 工具不存在或参数无效
        """
        ...

    def list_tool_names(self) -> list[str]:
        """列出所有工具名称"""
        return [t["function"]["name"] for t in self.get_tools()]
