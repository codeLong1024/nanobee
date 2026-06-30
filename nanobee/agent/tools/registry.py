"""工具注册表 - 动态工具管理与执行。

核心逻辑完全保留：注册、注销、查找、校验、执行、缓存。
新增 ToolPluginAdapter：将 ToolPlugin 适配为 Tool 接口。
"""

from __future__ import annotations

from typing import Any

from nanobee.agent.tools.base import Tool


class ToolPluginAdapter(Tool):
    """将 ToolPlugin 适配为 Tool 接口的适配器。

    使 PluginManager 注册的工具插件能无缝接入 ToolRegistry。
    """

    def __init__(self, plugin: Any, tool_def: dict[str, Any]) -> None:
        """初始化适配器。

        Args:
            plugin: ToolPlugin 实例
            tool_def: OpenAI function schema 格式的工具定义
        """
        self._plugin = plugin
        self._tool_def = tool_def
        self._func = tool_def.get("function", tool_def)

    @property
    def name(self) -> str:
        return str(self._func["name"])

    @property
    def description(self) -> str:
        return str(self._func.get("description", ""))

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(self._func.get("parameters", {"type": "object", "properties": {}}))

    async def execute(self, **kwargs: Any) -> Any:
        """执行工具（沙箱通过 ContextVar 注入，无需参数传递）

        Args:
            **kwargs: 工具参数

        Returns:
            工具执行结果
        """
        return await self._plugin.execute_tool(self.name, **kwargs)


class ToolRegistry:
    """Agent 工具注册中心。

    支持动态注册、注销、查找、校验和执行工具。
    内置工具和 MCP 工具分别排序，结果缓存直到下次 register/unregister。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> None:
        """注册工具实例。"""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """按名称注销工具。"""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """按名称查找工具。"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """从 OpenAI 或扁平 schema 中提取标准化工具名称。"""
        from nanobee.utils.helpers import extract_tool_name

        return extract_tool_name(schema)

    def get_definitions(self) -> list[dict[str, Any]]:
        """获取工具定义列表，内置工具优先排序，MCP 工具后置。

        结果缓存直到下次 register/unregister 调用。
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: dict[str, Any],
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        """解析、转换并校验单个工具调用。

        Returns:
            (tool, cast_params, error) 三元组，error 为 None 表示校验通过。
        """
        if not isinstance(params, dict) and name in ("write_file", "read_file"):
            return None, params, (
                f"Error: Tool '{name}' parameters must be a JSON object, got {type(params).__name__}. "
                "Use named parameters: tool_name(param1=\"value1\", param2=\"value2\")"
            )

        tool = self._tools.get(name)
        if not tool:
            return None, params, (
                f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            )
        return tool, cast_params, None

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """按名称和参数执行工具。

        Raises:
            ValueError: 参数校验失败或工具未找到。
            其他异常: 由工具插件直接传播，保留原始类型供 FaultClassifier 检测。
        """
        tool, params, error = self.prepare_call(name, params)
        if error:
            raise ValueError(error)
        assert tool is not None  # 由 prepare_call 保证
        return await tool.execute(**params)

    @property
    def tool_names(self) -> list[str]:
        """获取已注册工具名称列表。"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
