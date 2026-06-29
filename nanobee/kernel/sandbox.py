"""
上下文沙箱 — 强制文件操作在用户上下文根目录内执行

核心功能委托给 security.workspace_policy 中的纯函数，ContextSandbox
只做轻量根目录持有 + 参数清洗整合 + 元数据文件写保护。

支持多根白名单（read only roots），LLM 可读不可写，用于读取内置技能等只读资源。

受保护的元数据文件（_META_BLOCKED_FILES）：
- identity.yaml：用户身份配置，LLM 不可修改
- default.jsonl：会话历史（已在 .history/ 下保护）

受保护的目录（_META_BLOCKED_DIRS）：
- .history/：所有会话历史文件
- .tmp/：插件临时文件
- sessions/：会话 JSONL 文件（SessionManager 托管，LLM 不可读写删）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobee.exceptions import SandboxViolationError
from nanobee.security.workspace_policy import is_path_allowed, require_path_within

from nanobee.utils.logger import logger


# 元数据文件写保护 — LLM 不可读/写/删这些文件
_META_BLOCKED_FILES: frozenset[str] = frozenset({
    "identity.yaml",
    "default.jsonl",
})

# 隐藏目录写保护 — 整个目录不可访问
_META_BLOCKED_DIRS: frozenset[str] = frozenset({
    ".history",
    ".tmp",
    "sessions",
})




class ContextSandbox:
    """上下文沙箱 — 强制文件操作在 user context 内执行

    底层使用 security.workspace_policy 纯函数：
    - require_path_within: 路径边界校验
    - is_path_allowed: 多根路径允许判断

    支持读写根 + 只读根白名单：
    - writable_root：用户上下文根，可读写（== context_root）
    - read_only_roots：只读根列表，可读不可写（如内置技能目录）

    prefix_map：前缀 → 回退目录映射。
    "用户目录没找到，回退到内置目录"的 overlay 语义，
    曾分散在 tool_fs 插件的 _overlay_dirs 中，现统一到沙箱层。

    同时包含元数据文件/目录写保护：
    - identity.yaml：用户配置，LLM 不可修改
    - .history/ 下的所有文件：会话历史，LLM 不可修改
    - .tmp/ 下的所有文件：插件临时文件，LLM 不可修改
    - sessions/ 下的所有文件：会话 JSONL，LLM 不可修改（SessionManager 托管）

    单用户模式下可设为 None（不启用沙箱）。
    """

    def __init__(
        self,
        context_root: Path | str,
        read_only_roots: list[Path | str] | None = None,
        prefix_map: dict[str, Path | str] | None = None,
        process_workspace: Path | str | None = None,
    ) -> None:
        """初始化沙箱

        Args:
            context_root: 用户上下文根目录（绝对路径），可读写
            read_only_roots: 只读根目录列表（如内置技能目录），可读不可写
            prefix_map: 前缀 → 回退目录映射。
                当路径在可写根内不存在时，按前缀匹配回退到指定目录。
                例如 {"skills/": "/opt/nanobee/skills/"}。
            process_workspace: 子进程可写工作目录边界（如 context_root/workspace/）。
                用于 workspace 约束类型，execute_shell 的 working_dir 必须在此目录内。
                未设置时不校验（向后兼容）。
        """
        self._context_root = Path(context_root).resolve()
        self._read_only_roots = [Path(r).resolve() for r in (read_only_roots or [])]
        self._prefix_map = {
            k: Path(v).resolve() for k, v in (prefix_map or {}).items()
        }
        self._process_workspace = (
            Path(process_workspace).resolve() if process_workspace is not None else None
        )

    @property
    def context_root(self) -> Path:
        """用户上下文根目录（可读写）"""
        return self._context_root

    @property
    def read_only_roots(self) -> list[Path]:
        """只读根目录列表，如内置技能目录、实例技能目录"""
        return list(self._read_only_roots)

    def _all_roots(self) -> list[Path]:
        """所有可访问根 — 读写根 + 只读根"""
        return [self._context_root] + self._read_only_roots

    def _resolve(self, path_str: str, *, writable_only: bool = False) -> Path:
        """将路径解析为绝对路径，检查沙箱边界

        Args:
            path_str: 路径字符串
            writable_only: True=只允许在可写根（context_root）内；False=允许在所有根内

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        p = Path(path_str)
        if not p.is_absolute():
            p = (self._context_root / p).resolve()
        else:
            p = p.resolve()

        if writable_only:
            # 写操作：只允许在可写根（context_root）内
            require_path_within(str(p), self._context_root, message="写入路径逃逸拦截")
        else:
            # 读/列举操作：允许在所有根内
            if not is_path_allowed(str(p), self._all_roots()):
                raise SandboxViolationError(
                    path=str(p),
                    context_root=str(self._context_root),
                    detail="路径超出沙箱允许范围",
                )

        self._check_blocked(p)
        return p

    def resolve_safe(self, path_str: str) -> Path:
        """将路径解析为绝对路径，允许所有 roots（读/列举）

        相对路径基于 sandbox root 解析（而非 CWD），
        确保 memory/xxx 这类 skill 中常用的相对路径落在沙箱内。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        return self._resolve(path_str, writable_only=False)

    def resolve_safe_writable(self, path_str: str) -> Path:
        """将路径解析为绝对路径，仅允许可写根（写操作专用）

        写操作只能在 context_root 内进行，禁止对只读根写入。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        return self._resolve(path_str, writable_only=True)

    def resolve_safe_workspace(self, path_str: str) -> Path:
        """解析路径并约束在 process_workspace 边界内。

        相对路径基于 context_root 解析（与其他 resolve_* 一致），
        绝对路径直接解析。

        process_workspace 未设置时不校验边界（向后兼容）。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径

        Raises:
            SandboxViolationError: 路径超出进程工作目录边界
        """
        p = Path(path_str)
        if not p.is_absolute():
            p = (self._context_root / p).resolve()
        else:
            p = p.resolve()

        if self._process_workspace is not None:
            ws = self._process_workspace.resolve()
            if p != ws and ws not in p.parents:
                raise SandboxViolationError(
                    path=str(p),
                    context_root=str(self._context_root),
                    detail="路径超出进程工作目录边界",
                )

        self._check_blocked(p)
        return p

    def resolve_with_fallback(self, path_str: str) -> Path:
        """解析路径，支持前缀匹配回退（overlay 语义）。

        先在可写根 + 只读根内尝试路径。
        如果不存在，按 prefix_map 匹配前缀，回退到映射目录。

        典型场景：LLM 请求 "skills/foo.md"，
        用户目录 /ctx/skills/foo.md 不存在时，
        回退到内置目录 /opt/nanobee/skills/foo.md。

        Args:
            path_str: 路径字符串

        Returns:
            解析后的安全绝对路径（可能不存在，由调用方检查）

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        p = self._resolve(path_str, writable_only=False)
        if p.exists():
            return p

        # 前缀匹配回退
        if not self._prefix_map:
            return p
        norm_path = path_str.lstrip("/")
        for prefix, fallback_dir in self._prefix_map.items():
            norm_prefix = prefix.rstrip("/")
            if norm_path == norm_prefix:
                # 精确匹配前缀本身（如 "skills" → 列出内置 skills 目录）
                if fallback_dir.exists():
                    self._check_blocked(fallback_dir)
                    return fallback_dir.resolve()
            elif norm_path.startswith(norm_prefix + "/"):
                rel = Path(norm_path[len(norm_prefix) + 1:])
                candidate = (fallback_dir / rel).resolve()
                if candidate.exists():
                    self._check_blocked(candidate)
                    return candidate
        return p

    def sanitize_params(
        self,
        tool_name: str,
        params: dict[str, Any],
        param_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """清洗工具参数中的路径，根据 x-constraint 声明执行对应约束。

        三种内置约束类型（由工具在 JSON Schema 的 properties 中声明）：
        - "sandbox":   all roots 内可读，对应 resolve_safe()
        - "writable":  context_root 内可写，对应 resolve_safe_writable()
        - "workspace": process_workspace 内执行，对应 resolve_safe_workspace()

        无 x-constraint 声明的参数直通，框架不猜测参数语义。

        Args:
            tool_name: 工具名称（用于日志）
            params: 工具参数字典
            param_schema: 参数的 properties 字典（含 x-constraint 声明），
                通常来自 tool.parameters["properties"]。

        Returns:
            清洗后的参数字典（路径被替换为解析后的绝对路径）

        Raises:
            SandboxViolationError: 任意被约束的路径越界
        """
        if not isinstance(params, dict):
            return params

        properties = (param_schema or {}).get("properties", {})
        cleaned: dict[str, Any] = {}
        for key, value in params.items():
            if not isinstance(value, str):
                cleaned[key] = value
                continue

            constraint = properties.get(key, {}).get("x-constraint")
            if constraint == "workspace":
                cleaned[key] = str(self.resolve_safe_workspace(value))
            elif constraint == "writable":
                cleaned[key] = str(self.resolve_safe_writable(value))
            elif constraint == "sandbox":
                cleaned[key] = str(self.resolve_safe(value))
            else:
                cleaned[key] = value

        return cleaned

    def assert_allowed(self, path: Path | str) -> None:
        """断言路径在任意允许根内且不是受保护的元数据文件

        读/列举操作用此方法检查路径是否在可读写根或只读根内。

        Args:
            path: 待检查的路径

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        p = Path(path)
        self._check_blocked(p)
        if not is_path_allowed(str(p), self._all_roots()):
            raise SandboxViolationError(
                path=str(p),
                context_root=str(self._context_root),
                detail="路径越界断言失败",
            )

    def assert_allowed_writable(self, path: Path | str) -> None:
        """断言路径在可写根内（写入专用检查）

        Args:
            path: 待检查的路径

        Raises:
            SandboxViolationError: 路径越界或指向受保护的元数据文件
        """
        p = Path(path)
        self._check_blocked(p)
        require_path_within(str(p), self._context_root, message="写入路径断言失败")

    @staticmethod
    def _check_blocked(path: Path | str) -> None:
        """检查路径是否指向受保护的元数据文件或隐藏目录

        Args:
            path: 待检查的路径

        Raises:
            SandboxViolationError: 路径指向受保护的元数据文件或隐藏目录
        """
        p = Path(path)
        # 检查文件名黑名单
        if p.name in _META_BLOCKED_FILES:
            raise SandboxViolationError(
                path=str(p.resolve()),
                context_root="",
                detail=f"元数据文件受保护，禁止访问: {p.name}",
            )
        # 检查路径中是否包含受保护的隐藏目录
        resolved_parts = p.resolve().parts
        for part in resolved_parts:
            if part in _META_BLOCKED_DIRS:
                raise SandboxViolationError(
                    path=str(p.resolve()),
                    context_root="",
                    detail=f"隐藏目录受保护，禁止访问: {part}",
                )

    def __repr__(self) -> str:
        parts = [f"ContextSandbox(writable={self._context_root}"]
        if self._read_only_roots:
            parts.append(f"read_only={self._read_only_roots}")
        if self._process_workspace is not None:
            parts.append(f"process_workspace={self._process_workspace}")
        return ", ".join(parts) + ")"


__all__ = [
    "ContextSandbox",
]
