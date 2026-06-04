"""
上下文沙箱 — 强制文件操作在用户上下文根目录内执行

核心功能：
- resolve_safe(path_str)：将路径解析为绝对路径，若越界则抛出 SandboxError
- sanitize_params(tool_name, params)：在工具调用前清洗参数中的路径
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 包含路径的工具参数名（working_dir 特殊处理：只解析不拦截，拦截由 L2 工具层处理）
_PATH_PARAM_KEYS: frozenset[str] = frozenset({
    "path", "file_path", "directory", "dir", "target_path",
    "source", "destination", "src", "dst", "working_dir",
})

# working_dir 类参数名 — 只解析为绝对路径，不做沙箱拦截
_WORKING_DIR_KEYS: frozenset[str] = frozenset({"working_dir"})


class SandboxError(PermissionError):
    """沙箱拦截异常 — 文件操作超出用户上下文边界"""

    def __init__(self, path: str, context_root: str, detail: str = "") -> None:
        self.path = path
        self.context_root = context_root
        message = (
            f"沙箱拦截: 路径 {path!r} 超出用户上下文 {context_root!r}"
        )
        if detail:
            message += f"（{detail}）"
        super().__init__(message)


class ContextSandbox:
    """上下文沙箱 — 强制文件操作在 user context 内执行

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
        """将路径解析为绝对路径，若越界则抛出 SandboxError

        1. 将 path_str 解析为绝对路径（处理 .. 和符号链接）
        2. 检查是否在 context_root 下
        3. 如果越界则抛出异常

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxError: 路径越界
        """
        target = Path(path_str).resolve()

        # 检查目标路径是否以 context_root 开头
        try:
            target.relative_to(self._context_root)
        except ValueError:
            logger.warning(
                "沙箱拦截: %s 不在 %s 下",
                target, self._context_root,
            )
            raise SandboxError(
                path=str(target),
                context_root=str(self._context_root),
                detail="路径逃逸拦截",
            ) from None

        return target

    def sanitize_params(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """清洗工具参数中的路径，确保所有路径都在沙箱内

        对参数中所有已知的路径字段执行 resolve_safe 校验。
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
        """断言路径在沙箱内

        Args:
            path: 待检查的路径

        Raises:
            SandboxError: 路径越界
        """
        p = Path(path).resolve()
        try:
            p.relative_to(self._context_root)
        except ValueError:
            raise SandboxError(
                path=str(p),
                context_root=str(self._context_root),
                detail="路径越界断言失败",
            ) from None

    def __repr__(self) -> str:
        return f"ContextSandbox(root={self._context_root})"


__all__ = [
    "ContextSandbox",
    "SandboxError",
]
