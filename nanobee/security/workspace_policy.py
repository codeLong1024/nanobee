"""工作区路径边界工具

核心功能：
- resolve_path：解析路径，相对路径基于工作区
- is_path_within / is_path_allowed：检查路径是否在允许根目录内
- require_path_within：解析路径并要求其在根目录内
- resolve_allowed_path：解析路径并强制在允许根目录内

移植自 nanobot/security/workspace_policy.py（MIT License，保留上游版权）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from nanobee.exceptions import SandboxViolationError

WORKSPACE_BOUNDARY_NOTE = (
    "（这是硬性策略边界，非临时故障；"
    "不要使用 shell 技巧或替代工具重试，"
    "如果确实需要访问该资源，询问用户如何处理）"
)


def resolve_path(
    path: str | Path,
    workspace: str | Path | None = None,
    *,
    strict: bool = False,
) -> Path:
    """解析路径，相对路径基于 workspace 解释。

    Args:
        path: 待解析的路径
        workspace: 工作区根目录（可选，用于相对路径）
        strict: 是否严格模式（检查路径存在）

    Returns:
        解析后的绝对路径
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        candidate = Path(workspace).expanduser() / candidate
    return candidate.resolve(strict=strict)


def is_path_within(path: str | Path, root: str | Path) -> bool:
    """检查路径是否在 root 内（或等于 root）。

    Args:
        path: 待检查的路径
        root: 允许的根目录

    Returns:
        True 如果 path 在 root 内
    """
    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
        resolved_root = Path(root).expanduser().resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def is_path_allowed(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """检查路径是否在任意允许的根目录内。

    Args:
        path: 待检查的路径
        roots: 允许的根目录列表

    Returns:
        True 如果 path 在任意 roots 内
    """
    return any(is_path_within(path, root) for root in roots)


def require_path_within(
    path: str | Path,
    root: str | Path,
    *,
    message: str | None = None,
) -> Path:
    """解析路径并要求其在 root 内。

    Args:
        path: 待解析的路径
        root: 允许的根目录
        message: 自定义错误消息

    Returns:
        解析后的安全绝对路径

    Raises:
        SandboxViolationError: 路径超出 root
    """
    resolved = Path(path).expanduser().resolve(strict=False)
    if not is_path_within(resolved, root):
        raise SandboxViolationError(
            path=str(resolved),
            context_root=str(Path(root).expanduser().resolve(strict=False)),
            detail=message or "路径超出工作区边界",
        )
    return resolved


def resolve_allowed_path(
    path: str | Path,
    *,
    workspace: str | Path | None = None,
    allowed_root: str | Path | None = None,
    extra_allowed_roots: Iterable[str | Path] | None = None,
    strict: bool = False,
) -> Path:
    """解析路径并强制在允许根目录内。

    Args:
        path: 待解析的路径
        workspace: 工作区（用于相对路径）
        allowed_root: 主要允许根目录
        extra_allowed_roots: 额外允许的根目录
        strict: 是否严格模式

    Returns:
        解析后的安全绝对路径

    Raises:
        SandboxViolationError: 路径超出所有允许根目录
    """
    resolved = resolve_path(path, workspace, strict=False)
    if allowed_root is None:
        return resolve_path(path, workspace, strict=strict) if strict else resolved

    roots = [allowed_root, *(extra_allowed_roots or [])]
    if not is_path_allowed(resolved, roots):
        raise SandboxViolationError(
            path=str(resolved),
            context_root=str(Path(allowed_root).expanduser().resolve(strict=False)),
            detail="路径超出允许的工作区目录",
        )
    if strict:
        return resolve_path(path, workspace, strict=True)
    return resolved


__all__ = [
    "WORKSPACE_BOUNDARY_NOTE",
    "resolve_path",
    "is_path_within",
    "is_path_allowed",
    "require_path_within",
    "resolve_allowed_path",
]
