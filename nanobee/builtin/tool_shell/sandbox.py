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


def _bwrap(command: str, workspace: str, cwd: str, extra_ro_bind: list[str] | None = None) -> str:
    """使用 bubblewrap 包裹命令（需要系统安装 bwrap）。

    只将 workspace 目录 bind-mount 为可读写，其父目录以 tmpfs 遮掩，
    阻止子进程访问其他用户目录和敏感配置。

    Args:
        command: 原始 shell 命令
        workspace: 可读写的工作区根目录（子进程唯一可写入的地方）
        cwd: 容器内的工作目录（通常与 workspace 相同或为其子目录）
        extra_ro_bind: 额外只读挂载路径列表（如启用的实例技能目录）

    Returns:
        包裹后的 bwrap 命令字符串
    """
    ws = Path(workspace).resolve()

    # 计算容器内的 cwd（相对于 workspace）
    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    # 系统必须的可读目录
    required = ["/usr"]
    # 尝试挂载的可选系统目录
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
        "--tmpfs", str(home),             # 掩藏整个 $HOME（阻止访问配置/SSH 密钥等）
    ]
    # 额外只读挂载（启用的实例技能目录）—— 部署方通过 skills.enabled 声明
    # 必须在 --tmpfs $HOME 之后，否则被 tmpfs 掩藏覆盖
    # 若目标路径在被 tmpfs 遮盖的 HOME 下，先建目录再绑定
    for p in (extra_ro_bind or []):
        resolved = str(Path(p).expanduser().resolve())
        if not Path(resolved).exists():
            continue
        if resolved.startswith(str(home)):
            args += ["--dir", resolved]
        args += ["--ro-bind", resolved, resolved]
    args += [
        "--dir", str(ws),                 # 重建 workspace 挂载点
        "--bind", str(ws), str(ws),       # workspace 可读写
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
) -> str:
    """使用命名沙箱后端包裹命令。

    Args:
        sandbox: 后端名称（如 "bwrap"）
        command: 原始 shell 命令
        workspace: 可读写的工作区根目录
        cwd: 当前工作目录
        extra_ro_bind: 额外只读挂载路径列表（启用的实例技能目录）

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

    result = backend_fn(command, workspace, cwd, extra_ro_bind=extra_ro_bind)
    logger.debug("沙箱包裹命令 (backend={}): {}", sandbox, result[:200])
    return result


__all__ = [
    "wrap_command",
    "_BACKENDS",
]
