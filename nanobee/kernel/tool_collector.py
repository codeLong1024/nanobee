"""
工具收集器 — 按用户权限过滤可用工具

规则：
- 如果 whitelist 非空：工具必须在 whitelist 中
- 如果 blacklist 非空：工具不能在 blacklist 中
- 如果 whitelist 为空且 blacklist 为空：全部可用（兼容单用户）
"""

from __future__ import annotations

from typing import Any

from nanobee.utils.logger import logger



class ToolCollector:
    """工具收集器 — 按用户权限过滤可用工具

    双重过滤：
    1. 定义层：过滤 tool definitions，LLM 看不到禁用工具（避免幻觉）
    2. 执行层：检查工具名是否在允许列表中（后备防线）
    """

    def __init__(
        self,
        tool_names: list[str],
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
    ) -> None:
        """初始化收集器

        Args:
            tool_names: 全局可用工具名列表（从 ToolRegistry 获取）
            whitelist: 白名单（None 或空列表 = 全部允许）
            blacklist: 黑名单（None 或空列表 = 无禁用）
        """
        self._all_tools = list(tool_names)
        self._whitelist = set(whitelist or [])
        self._blacklist = set(blacklist or [])

    @property
    def allowed_tools(self) -> list[str]:
        """获取允许的工具名列表"""
        if self._whitelist:
            # 白名单模式：取交集
            allowed = [t for t in self._all_tools if t in self._whitelist]
        else:
            # 无白名单：全部允许
            allowed = list(self._all_tools)

        # 应用黑名单
        if self._blacklist:
            allowed = [t for t in allowed if t not in self._blacklist]

        return allowed

    def is_allowed(self, tool_name: str) -> bool:
        """检查工具是否允许执行

        Args:
            tool_name: 工具名

        Returns:
            True 表示允许执行
        """
        return tool_name in self.allowed_tools

    def filter_definitions(
        self,
        definitions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """过滤工具定义列表（OpenAI function schema 格式）

        Args:
            definitions: 全局工具定义列表

        Returns:
            过滤后的工具定义列表
        """
        allowed = set(self.allowed_tools)
        filtered: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name in allowed:
                filtered.append(schema)
        return filtered

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """从 schema 中提取工具名称"""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    @property
    def has_restrictions(self) -> bool:
        """是否有任何限制（白名单或黑名单非空）"""
        return bool(self._whitelist) or bool(self._blacklist)

    def __repr__(self) -> str:
        allowed = self.allowed_tools
        return (
            f"ToolCollector(allowed={len(allowed)}/{len(self._all_tools)}, "
            f"whitelist={len(self._whitelist)}, blacklist={len(self._blacklist)})"
        )


__all__ = [
    "ToolCollector",
]
