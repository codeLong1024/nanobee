"""
上下文沙箱 — 强制文件操作在用户上下文根目录内执行

核心功能委托给 security.workspace_policy 中的纯函数，ContextSandbox
只做轻量根目录持有 + 参数清洗整合 + 元数据文件写保护。

受保护的元数据文件（_META_BLOCKED_FILES）：
- context.yaml：用户配置，LLM 不可修改
- history.jsonl：对话历史，LLM 不可修改
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobee.exceptions import SandboxViolationError
from nanobee.security.workspace_policy import require_path_within

from nanobee.utils.logger import logger


# 包含路径的工具参数名（working_dir 特殊处理：只解析不拦截，拦截由 L2 工具层处理）
_PATH_PARAM_KEYS: frozenset[str] = frozenset({
    "path", "file_path", "directory", "dir", "target_path",
    "source", "destination", "src", "dst", "working_dir",
})

# working_dir 类参数名 — 只解析为绝对路径，不做沙箱拦截
_WORKING_DIR_KEYS: frozenset[str] = frozenset({"working_dir"})

# 元数据文件写保护 — LLM 不可读/写/删这些文件
_META_BLOCKED_FILES: frozenset[str] = frozenset({
    "context.yaml",
    "history.jsonl",
})

# 向后兼容别名
SandboxError = SandboxViolationError


class ContextSandbox:
    """上下文沙箱 — 强制文件操作在 user context 内执行

    底层使用 security.workspace_policy 纯函数：
    - require_path_within: 路径边界校验
    - resolve_path: 路径解析

    同时包含元数据文件写保护：
    - context.yaml / history.jsonl 被 LLM 访问时抛出 SandboxError

    单用户模式下可设为 None（不启用沙箱）。
    """

    def __init__(self, context_root: Path | str) -> None:
        """初始化沙箱

        Args:
            context_root: 用户上下文根目录（绝对路径）
        """
        self._context_root = Path(context_root).resolve()

    @property
    def context_root(self) -> Path:
        """用户上下文根目录"""
        return self._context_root

    def resolve_safe(self, path_str: str) -> Path:
        """将路径解析为绝对路径，若越界或指向受保护的元数据文件则抛出 SandboxError

        相对路径基于 sandbox root 解析（而非 CWD），
        确保 memory/xxx 这类 skill 中常用的相对路径落在沙箱内。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxError: 路径越界或指向受保护的元数据文件
        """
        p = Path(path_str)
        if not p.is_absolute():
            p = (self._context_root / p).resolve()
        else:
            p = p.resolve()
        safe_path = require_path_within(str(p), self._context_root, message="路径逃逸拦截")
        self._check_blocked(safe_path)
        return safe_path

    def sanitize_params(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """清洗工具参数中的路径，确保所有路径都在沙箱内

        对参数中所有已知的路径字段执行沙箱校验。
        working_dir 参数特殊处理：只解析为绝对路径，不做沙箱拦截
        （拦截由 L2 工具层处理，因为 working_dir 可能指向项目根）。

        Args:
            tool_name: 工具名称（用于日志）
            params: 工具参数字典

        Returns:
            清洗后的参数字典（路径被替换为解析后的绝对路径）

        Raises:
            SandboxError: 任意路径越界
        """
        if not isinstance(params, dict):
            return params

        cleaned: dict[str, Any] = {}
        for key, value in params.items():
            if key in _WORKING_DIR_KEYS and isinstance(value, str):
                # working_dir 只解析为绝对路径，不做沙箱拦截
                try:
                    cleaned[key] = str(Path(value).resolve())
                except Exception:
                    cleaned[key] = value
            elif key in _PATH_PARAM_KEYS and isinstance(value, str):
                safe_path = self.resolve_safe(value)
                cleaned[key] = str(safe_path)
            else:
                cleaned[key] = value

        return cleaned

    def assert_allowed(self, path: Path | str) -> None:
        """断言路径在沙箱内且不是受保护的元数据文件

        委托给 require_path_within 纯函数。

        Args:
            path: 待检查的路径

        Raises:
            SandboxError: 路径越界或指向受保护的元数据文件
        """
        self._check_blocked(path)
        require_path_within(path, self._context_root, message="路径越界断言失败")

    @staticmethod
    def _check_blocked(path: Path | str) -> None:
        """检查路径是否指向受保护的元数据文件

        Args:
            path: 待检查的路径

        Raises:
            SandboxError: 路径指向受保护的元数据文件
        """
        p = Path(path)
        if p.name in _META_BLOCKED_FILES:
            raise SandboxViolationError(
                path=str(p.resolve()),
                context_root="",
                detail=f"元数据文件受保护，禁止访问: {p.name}",
            )

    def __repr__(self) -> str:
        return f"ContextSandbox(root={self._context_root})"


__all__ = [
    "ContextSandbox",
    "SandboxError",
]
