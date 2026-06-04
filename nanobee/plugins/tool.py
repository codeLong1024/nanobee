"""ToolPlugin 接口 - 工具插件"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any

from nanobee.plugins.base import NanobeePlugin

logger = logging.getLogger(__name__)


class ToolPlugin(NanobeePlugin):
    """工具插件基类

    每个工具插件可以提供一个或多个工具给 Agent 使用。

    支持通过 sandbox 属性注入 user-context 沙箱，实现防御纵深。
    """

    plugin_type = "tool"

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata)
        # 可选的沙箱实例（由 runner 按请求注入，用于防御纵深）
        self._sandbox: Any | None = None

    @property
    def sandbox(self) -> Any | None:
        """获取当前沙箱实例"""
        return self._sandbox

    @sandbox.setter
    def sandbox(self, value: Any | None) -> None:
        """设置沙箱实例（按请求注入）"""
        self._sandbox = value

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
