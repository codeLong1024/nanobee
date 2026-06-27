"""
Shell 命令沙箱后端 — 进程级隔离

将原始 shell 命令包裹在沙箱命令中（如 bwrap），使子进程在受限的
mount namespace 内执行，防止通过子进程绕过工具层的 ContextSandbox。

添加新后端：实现签名一致的函数 _wrap_<name>(command, workspace, cwd) → str，
并在 _BACKENDS 中注册。
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable

from nanobee.utils.logger import logger


def _ensure_parent_dirs(args: list[str], resolved: str, home: Path) -> None:
    """若路径在 tmpfs 覆盖的家目录下，逐级创建 --dir 穿透 tmpfs。

    bwrap 使用 ``--tmpfs HOME`` 隐藏真实家目录内容。
    挂载 HOME 下任意路径前必须先 --dir 重建各级祖先目录，
    否则 --bind/--ro-bind 的目标父目录不存在，挂载失败。

    不在 HOME 下的路径直接从宿主编译空间继承，无需 --dir。
    """
    home_str = str(home)
    if not resolved.startswith(home_str + "/"):
        return
    # HOME 下路径：逐级 --dir 穿透 tmpfs
    rel = Path(resolved).relative_to(home)
    current = home
    for part in rel.parts:
        current = current / part
        args += ["--dir", str(current)]


def _bwrap(
    command: str,
    workspace: str,
    cwd: str,
    extra_ro_bind: list[str] | None = None,
    extra_rw_bind: list[str] | None = None,
) -> str:
    """使用 bubblewrap 包裹命令（需要系统安装 bwrap）。

    只暴露 workspace 目录为可读写，HOME 以 tmpfs 遮掩，
    阻止子进程访问其他用户目录和敏感配置。

    Args:
        command: 原始 shell 命令
        workspace: 可读写的工作区根目录
        cwd: 容器内的工作目录
        extra_ro_bind: 额外只读挂载列表，支持两种格式：
            - 纯路径: ``"/real/path"`` → 绑定到相同路径
            - source:target: ``"/real/path:/sandbox/path"`` → 绑定到容器内非 HOME 路径
        extra_rw_bind: 额外可读写挂载列表（纯路径）

    Returns:
        包裹后的 bwrap 命令字符串
    """
    ws = Path(workspace).resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        raise ValueError(
            f"沙箱工作目录不在 workspace 内: cwd={cwd}, workspace={workspace}"
        ) from None

    required = ["/usr"]
    optional = [
        "/bin", "/lib", "/lib64", "/etc/alternatives",
        "/etc/ssl/certs", "/etc/resolv.conf", "/etc/ld.so.cache",
    ]

    home = Path.home()
    args = ["bwrap", "--new-session", "--die-with-parent"]
    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        if Path(p).exists():
            args += ["--ro-bind", p, p]
    args += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", str(home),
    ]

    # ── 额外只读挂载 ──────────────────────────────────────────────
    for p in (extra_ro_bind or []):
        # source:target 格式：宿主编译路径映射到容器内非 HOME 路径（如 venv）
        if ":" in p:
            parts = p.split(":", 1)
            source = str(Path(parts[0]).expanduser().resolve())
            if Path(source).exists() and parts[1].startswith("/"):
                target = parts[1]
                for parent_part in reversed(Path(target).parents):
                    if parent_part != Path("/"):
                        args += ["--dir", str(parent_part)]
                args += ["--ro-bind", source, target]
                continue

        # 纯路径：挂载到相同路径
        resolved = str(Path(p).expanduser().resolve())
        if not Path(resolved).exists():
            continue
        _ensure_parent_dirs(args, resolved, home)
        args += ["--ro-bind", resolved, resolved]

    # ── 可读写挂载：workspace + 额外 rw 路径 ─────────────────────────
    _ensure_parent_dirs(args, str(ws), home)
    args += ["--bind", str(ws), str(ws)]

    for p in (extra_rw_bind or []):
        resolved = str(Path(p).expanduser().resolve())
        if not Path(resolved).exists():
            continue
        _ensure_parent_dirs(args, resolved, home)
        args += ["--bind", resolved, resolved]

    args += [
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]
    return shlex.join(args)


# 后端注册表：名称 → (可调用, 依赖检查函数)
# 依赖检查函数返回 (available: bool, error_msg: str | None)
# 后端函数签名: (command, workspace, cwd, extra_ro_bind=None) → str
_BACKENDS: dict[str, tuple[Callable[..., str], Callable[[], tuple[bool, str | None]]]] = {
    "bwrap": (
        _bwrap,
        lambda: (
            _check_available("bwrap"),
            "bwrap 未安装。请安装: apt install bubblewrap 或 brew install bwrap"
            if not _check_available("bwrap") else None,
        ),
    ),
}


def _check_available(name: str) -> bool:
    """检查系统命令是否可用"""
    import shutil
    return shutil.which(name) is not None


def wrap_command(
    sandbox: str,
    command: str,
    workspace: str,
    cwd: str,
    extra_ro_bind: list[str] | None = None,
    extra_rw_bind: list[str] | None = None,
) -> str:
    """使用命名沙箱后端包裹命令。

    Args:
        sandbox: 后端名称（如 "bwrap"）
        command: 原始 shell 命令
        workspace: 可读写的工作区根目录
        cwd: 当前工作目录
        extra_ro_bind: 额外只读挂载路径列表（启用的实例技能目录）
        extra_rw_bind: 额外可读写挂载路径列表（用户 skills_dir）

    Returns:
        包裹后的命令字符串

    Raises:
        ValueError: 未知的后端名称
        RuntimeError: 后端依赖不可用
    """
    backend_entry = _BACKENDS.get(sandbox)
    if backend_entry is None:
        raise ValueError(
            f"未知的沙箱后端 {sandbox!r}。可用后端: {list(_BACKENDS)}"
        )

    backend_fn, dep_check = backend_entry
    available, error_msg = dep_check()
    if not available:
        raise RuntimeError(error_msg)

    result = backend_fn(command, workspace, cwd, extra_ro_bind=extra_ro_bind, extra_rw_bind=extra_rw_bind)
    logger.debug("沙箱包裹命令 (backend={}): {}", sandbox, result[:200])
    return result


__all__ = [
    "wrap_command",
    "_BACKENDS",
]
