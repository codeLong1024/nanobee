"""
Tool FS 插件 - 文件系统工具（read_file, write_file, edit_file, list_dir）
基于 nanobot/agent/tools/filesystem.py 适配 nanobee 插件架构
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from nanobee.plugins.tool import ToolPlugin

logger = logging.getLogger(__name__)


class ToolFileSystemPlugin(ToolPlugin):
    """文件系统工具插件"""

    name = "tool-fs"
    version = "1.0.0"
    plugin_type = "tool"

    def __init__(self, metadata: Any = None, workspace: str | None = None):
        super().__init__(metadata)
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._allowed_dir: Path | None = None

    def get_tools(self) -> list[dict[str, Any]]:
        """获取工具定义列表（OpenAI function schema 格式）

        Returns:
            工具定义列表，包含 read_file, write_file, edit_file, list_dir
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": self._read_file_desc(),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "文件路径",
                            },
                            "offset": {
                                "type": "integer",
                                "description": "起始行号（从 1 开始，默认 1）",
                                "minimum": 1,
                            },
                            "limit": {
                                "type": "integer",
                                "description": "最大读取行数（默认 2000）",
                                "minimum": 1,
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "创建新文件或覆盖整个文件内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "文件路径",
                            },
                            "content": {
                                "type": "string",
                                "description": "写入的内容",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "精确替换文件中的指定文本",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "文件路径",
                            },
                            "old_text": {
                                "type": "string",
                                "description": "要查找的文本",
                            },
                            "new_text": {
                                "type": "string",
                                "description": "替换后的文本",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "是否替换所有匹配项（默认 false）",
                            },
                        },
                        "required": ["path", "old_text", "new_text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": "列出目录内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "目录路径",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "是否递归列出（默认 false）",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
        ]

    def _read_file_desc(self) -> str:
        """读取文件工具描述"""
        return (
            "读取文件（文本）。文本输出格式：LINE_NUM|CONTENT。"
            "对大文件使用 offset 和 limit 分页读取。"
            "读取前建议使用 list_dir 确认路径。"
            "读取内容超过 128K 字符会被截断。"
        )

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
        if tool_name == "read_file":
            return await self._execute_read_file(**kwargs)
        elif tool_name == "write_file":
            return await self._execute_write_file(**kwargs)
        elif tool_name == "edit_file":
            return await self._execute_edit_file(**kwargs)
        elif tool_name == "list_dir":
            return await self._execute_list_dir(**kwargs)
        else:
            raise ValueError(f"未知工具: {tool_name}")

    # ------------------------------------------------------------------
    # read_file 实现
    # ------------------------------------------------------------------

    async def _execute_read_file(
        self,
        path: str | None = None,
        offset: int = 1,
        limit: int | None = None,
        **kwargs: Any,
    ) -> str:
        """读取文件内容

        Args:
            path: 文件路径
            offset: 起始行号（从 1 开始）
            limit: 最大读取行数

        Returns:
            文件内容字符串，格式为 LINE_NUM|CONTENT
        """
        try:
            if not path:
                return "错误：未知文件路径"

            fp = self._resolve_path(path)
            if not fp.exists():
                return f"错误：文件不存在: {path}"
            if not fp.is_file():
                return f"错误：不是文件: {path}"

            raw = fp.read_bytes()
            if not raw:
                return f"（空文件: {path}）"

            try:
                text_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"错误：无法读取二进制文件 {path}"

            # 规范化换行符（CRLF -> LF）
            text_content = text_content.replace("\r\n", "\n")

            all_lines = text_content.splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return f"错误：offset {offset} 超出文件末尾（共 {total} 行）"

            start = offset - 1
            end = min(start + (limit or 2000), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if end < total:
                result += f"\n\n（显示第 {offset}-{end} 行，共 {total} 行。使用 offset={end + 1} 继续读取。）"
            else:
                result += f"\n\n（文件结尾 — 共 {total} 行）"

            return result

        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"读取文件失败: {e}"

    # ------------------------------------------------------------------
    # write_file 实现
    # ------------------------------------------------------------------

    async def _execute_write_file(
        self,
        path: str | None = None,
        content: str | None = None,
        **kwargs: Any,
    ) -> str:
        """写入文件内容

        Args:
            path: 文件路径
            content: 写入的内容

        Returns:
            写入结果消息
        """
        try:
            if not path:
                raise ValueError("未知文件路径")
            if content is None:
                raise ValueError("未知内容")

            fp = self._resolve_path(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")

            return f"成功写入 {len(content)} 个字符到 {fp}"

        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"写入文件失败: {e}"

    # ------------------------------------------------------------------
    # edit_file 实现
    # ------------------------------------------------------------------

    async def _execute_edit_file(
        self,
        path: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str:
        """编辑文件（精确替换文本）

        Args:
            path: 文件路径
            old_text: 要查找的文本
            new_text: 替换后的文本
            replace_all: 是否替换所有匹配项

        Returns:
            编辑结果消息
        """
        try:
            if not path:
                raise ValueError("未知文件路径")
            if old_text is None:
                raise ValueError("未知 old_text")
            if new_text is None:
                raise ValueError("未知 new_text")

            fp = self._resolve_path(path)

            # 创建文件语义：old_text='' 且文件不存在 → 创建
            if not fp.exists():
                if old_text == "":
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(new_text, encoding="utf-8")
                    return f"成功创建文件 {fp}"
                return f"错误：文件不存在: {path}"

            # 读取文件
            raw = fp.read_bytes()
            content = raw.decode("utf-8").replace("\r\n", "\n")
            norm_old = old_text.replace("\r\n", "\n")

            # 查找匹配
            matches = self._find_matches(content, norm_old)
            if not matches:
                preview = old_text[:50].replace("\n", "\\n")
                return f"错误：在 {path} 中未找到文本: {preview}..."

            count = len(matches)
            if count > 1 and not replace_all:
                line_numbers = [match["line"] for match in matches[:3]]
                return (
                    f"警告：old_text 匹配了 {count} 次"
                    f"（第 {', '.join(map(str, line_numbers))} 行）"
                    "。设置 replace_all=true 替换所有，或使用 old_text 包含更多上下文。"
                )

            # 执行替换
            selected = matches if replace_all else [matches[0]]
            new_content = content
            for match in reversed(selected):
                new_content = new_content[:match["start"]] + new_text + new_content[match["end"]:]

            fp.write_bytes(new_content.encode("utf-8"))
            return f"成功编辑文件 {fp}"

        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"编辑文件失败: {e}"

    # ------------------------------------------------------------------
    # list_dir 实现
    # ------------------------------------------------------------------

    async def _execute_list_dir(
        self,
        path: str | None = None,
        recursive: bool = False,
        **kwargs: Any,
    ) -> str:
        """列出目录内容

        Args:
            path: 目录路径
            recursive: 是否递归列出

        Returns:
            目录内容字符串
        """
        try:
            if path is None:
                raise ValueError("未知路径")

            dp = self._resolve_path(path)
            if not dp.exists():
                return f"错误：目录不存在: {path}"
            if not dp.is_dir():
                return f"错误：不是目录: {path}"

            ignore_dirs = {
                ".git", "node_modules", "__pycache__", ".venv", "venv",
                "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
            }

            items: list[str] = []

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in ignore_dirs for p in item.parts):
                        continue
                    rel = item.relative_to(dp)
                    items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in ignore_dirs:
                        continue
                    prefix = "📁 " if item.is_dir() else "📄 "
                    items.append(f"{prefix}{item.name}")

            if not items:
                return f"目录 {path} 为空"

            return "\n".join(items)

        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"列出目录失败: {e}"

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str) -> Path:
        """解析文件路径，支持相对路径转换为绝对路径

        Args:
            path: 文件路径（可以是相对或绝对路径）

        Returns:
            解析后的绝对路径
        """
        p = Path(path)
        if not p.is_absolute():
            p = self._workspace / p
        return p.resolve()

    @staticmethod
    def _find_matches(content: str, old_text: str) -> list[dict[str, Any]]:
        """查找所有匹配位置

        Args:
            content: 文件内容
            old_text: 要查找的文本

        Returns:
            匹配列表，每个元素包含 start, end, line
        """
        matches = []
        start = 0
        while True:
            idx = content.find(old_text, start)
            if idx == -1:
                break
            line_num = content.count("\n", 0, idx) + 1
            matches.append({
                "start": idx,
                "end": idx + len(old_text),
                "line": line_num,
            })
            start = idx + max(1, len(old_text))
        return matches
