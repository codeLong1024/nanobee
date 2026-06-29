"""
Tool FS 插件 - 文件系统工具（read_file, write_file, edit_file, list_dir）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nanobee.exceptions import SandboxViolationError
from nanobee.plugins import ToolPlugin

from nanobee.utils.logger import logger


class ToolFileSystemPlugin(ToolPlugin):
    """文件系统工具插件"""

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)

    def get_tools(self) -> list[dict[str, Any]]:
        """获取工具定义列表（OpenAI function schema 格式）"""
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
                                "x-constraint": "sandbox",
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
                                "x-constraint": "writable",
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
                                "x-constraint": "writable",
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
                                "x-constraint": "sandbox",
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
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": (
                        "删除文件或目录。支持递归删除目录及其所有内容。"
                        "路径受 writable 沙箱约束保护，删除操作不可撤销，请谨慎使用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "要删除的文件或目录路径",
                                "x-constraint": "writable",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "删除目录时是否递归删除（默认 false，仅允许删除空目录）",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
        ]

    def _read_file_desc(self) -> str:
        return (
            "读取文件（文本）。文本输出格式：LINE_NUM|CONTENT。"
            "对大文件使用 offset 和 limit 分页读取。"
            "读取前建议使用 list_dir 确认路径。"
            "支持自动回退到内置模板：用户目录不存在时自动读取内置文件"
            "（如 skills/ 目录优先读用户版，不存在则回退到内置版本）。"
            "读取内容超过 100K 字符会被截断，可使用 offset 缩小范围。"
        )

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> str:
        """执行指定工具"""
        if tool_name == "read_file":
            return await self._execute_read_file(**kwargs)
        elif tool_name == "write_file":
            return await self._execute_write_file(**kwargs)
        elif tool_name == "edit_file":
            return await self._execute_edit_file(**kwargs)
        elif tool_name == "list_dir":
            return await self._execute_list_dir(**kwargs)
        elif tool_name == "delete_file":
            return await self._execute_delete_file(**kwargs)
        else:
            raise ValueError(f"未知工具: {tool_name}")

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
            fp = self._resolve_and_check(path)
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

            # 按字符截断，防止单行极长时结果过大触发持久化
            _MAX_READ_CHARS = 100_000
            if len(result) > _MAX_READ_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > _MAX_READ_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n（显示第 {offset}-{end} 行，共 {total} 行。使用 offset={end + 1} 继续读取。）"
            else:
                result += f"\n\n（文件结尾 — 共 {total} 行）"

            return result

        except SandboxViolationError as e:
            return f"错误：沙箱拦截 - {e}"
        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"读取文件失败: {e}"

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
            fp = self._resolve_and_check(path, writable=True)
            if content is None:
                raise ValueError("未知内容")
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")

            return f"成功写入 {len(content)} 个字符到 {fp}"

        except SandboxViolationError as e:
            return f"错误：沙箱拦截 - {e}"
        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"写入文件失败: {e}"

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
            fp = self._resolve_and_check(path, writable=True)
            if old_text is None:
                raise ValueError("未知 old_text")
            if new_text is None:
                raise ValueError("未知 new_text")

            # 创建文件语义：old_text='' 且文件不存在时创建
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

        except SandboxViolationError as e:
            return f"错误：沙箱拦截 - {e}"
        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"编辑文件失败: {e}"

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
            dp = self._resolve_and_check(path)
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
                    items.append(f"{item.name}/" if item.is_dir() else item.name)

            if not items:
                return f"目录 {path} 为空"

            return "\n".join(items)

        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except SandboxViolationError as e:
            return f"错误：沙箱拦截 - {e}"
        except Exception as e:
            return f"列出目录失败: {e}"

    def _resolve_and_check(self, path: str | None, *, writable: bool = False) -> Path:
        """解析路径并执行基础校验（空值检查 + 沙箱解析），消除各 execute 方法重复。

        Args:
            path: 文件或目录路径（可以是相对或绝对路径，不能为 None/空）
            writable: True 使用可写根校验，False 使用可读根 + overlay 回退

        Returns:
            沙箱校验后的安全绝对路径

        Raises:
            ValueError: path 为空
            SandboxViolationError: 路径越界
        """
        if not path:
            raise ValueError("未知路径")
        return self._resolve_sandbox_path(path, writable=writable)

    def _resolve_sandbox_path(self, path: str, *, writable: bool = False) -> Path:
        """沙箱路径解析。

        通过 ContextVar 获取当前任务沙箱实例，根据 writable 参数选择校验策略：
        - writable=False：可读根 + overlay 回退（用于 read_file / list_dir）
        - writable=True：仅允许可写根（用于 write_file / edit_file / delete_file）

        Raises:
            SandboxViolationError: 路径越界
        """
        from nanobee.kernel.context_sandbox_var import current_sandbox

        sandbox = current_sandbox()
        if sandbox is not None:
            if writable:
                return sandbox.resolve_safe_writable(path)
            return sandbox.resolve_with_fallback(path)

        # 无沙箱时回退到普通路径解析
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return p

    async def _execute_delete_file(
        self,
        path: str | None = None,
        recursive: bool = False,
        **kwargs: Any,
    ) -> str:
        """删除文件或目录。

        Args:
            path: 要删除的文件或目录路径
            recursive: 删除目录时是否递归删除（默认 false，仅允许删除空目录）

        Returns:
            操作结果消息
        """
        try:
            import shutil
            fp = self._resolve_and_check(path, writable=True)
            if not fp.exists():
                return f"错误：路径不存在: {path}"

            if fp.is_file():
                fp.unlink()
                return f"成功删除文件: {fp}"
            elif fp.is_dir():
                if recursive:
                    shutil.rmtree(fp)
                    return f"成功递归删除目录: {fp}"
                else:
                    try:
                        fp.rmdir()
                        return f"成功删除空目录: {fp}"
                    except OSError:
                        items = list(fp.iterdir())
                        preview = ", ".join(p.name for p in items[:5])
                        more = f" 等共 {len(items)} 项" if len(items) > 5 else ""
                        return (
                            f"错误：目录非空: {fp}（包含: {preview}{more}）。"
                            " 使用 recursive=true 递归删除。"
                        )
            else:
                return f"错误：不支持的文件类型: {path}"
        except SandboxViolationError as e:
            return f"错误：沙箱拦截 - {e}"
        except PermissionError as e:
            return f"错误：权限不足 - {e}"
        except Exception as e:
            return f"删除失败: {e}"

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
