"""
Tool Shell 插件 - Shell 命令工具（execute_shell, write_stdin）

基于 nanobot/agent/tools/shell.py 适配 nanobee 插件架构。
沙箱通过 ContextVar 注入（见 context_sandbox_var.py），消除逐层参数透传。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobee.builtin.tool_shell.sandbox import wrap_command as _wrap_sandbox_command
from nanobee.kernel.context_sandbox_var import current_sandbox as _current_sandbox
from nanobee.plugins.tool import ToolPlugin
from nanobee.security.network import contains_internal_url

from nanobee.utils.logger import logger


_IS_WINDOWS = sys.platform == "win32"

# 策略提示：追加到可恢复的工作区边界守卫错误后
_WORKSPACE_BOUNDARY_NOTE = (
    "\n\n注意：这是硬性策略边界，非临时故障。"
    "不要使用 shell 技巧（符号链接、base64 管道、替代工具、working_dir 覆盖）重试。"
    "如果用户确实需要访问该资源，告知在当前 restrict_to_workspace 策略下无法访问，询问如何处理。"
)

# 危险命令 deny 模式（不区分大小写匹配）
_DENY_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\b",            # rm -r, rm -rf, rm -fr
    r"\bdel\s+/[fq]\b",                # del /f, del /q
    r"\brmdir\s+/s\b",                 # rmdir /s
    r"(?:^|[;&|]\s*)format(?!=)\b",    # format（作为独立命令）
    r"\b(mkfs|diskpart)\b",            # 磁盘操作
    r"\bdd\s+if=",                     # dd
    r">\s*/dev/sd",                    # 写入磁盘设备
    r"\b(shutdown|reboot|poweroff)\b", # 系统电源
    r":\(\)\s*\{.*\};\s*:",            # fork 炸弹
]

# 内核设备文件 — 安全的重定向目标
_BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
    "/dev/null", "/dev/zero", "/dev/full",
    "/dev/random", "/dev/urandom",
    "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/tty",
})


@dataclass(slots=True)
class _PreparedCommand:
    """准备好的命令信息"""
    command: str
    cwd: str
    env: dict[str, str]
    timeout: int | None
    shell_program: str | None
    login: bool


class ToolShellPlugin(ToolPlugin):
    """Shell 工具插件 — 提供 execute_shell 和 write_stdin 工具

    支持双层沙箱校验：
    - L1: restrict_to_workspace 启用时，以 _workspace 为边界校验路径
    - L2: ContextVar 注入的 ContextSandbox 优先，实现防御纵深
    """

    name = "tool_shell"
    version = "1.0.0"
    plugin_type = "tool"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    def __init__(
        self,
        metadata: Any = None,
        workspace: str | None = None,
        restrict_to_workspace: bool = False,
    ):
        super().__init__(metadata)
        self._workspace = str(Path(workspace).resolve()) if workspace else os.getcwd()
        self.restrict_to_workspace = restrict_to_workspace
        self._default_timeout = 60

    # ------------------------------------------------------------------
    # ToolPlugin 接口
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """获取工具定义列表（OpenAI function schema 格式）

        Returns:
            工具定义列表，包含 execute_shell
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "execute_shell",
                    "description": self._execute_shell_desc(),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "要执行的 shell 命令",
                            },
                            "working_dir": {
                                "type": "string",
                                "description": "命令的工作目录（可选，默认使用工作区根目录）",
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "超时秒数（默认 60，最大 600）",
                                "minimum": 1,
                                "maximum": 600,
                            },
                            "shell": {
                                "type": "string",
                                "description": "指定 shell 解释器（sh, bash, zsh 或绝对路径，可选）",
                            },
                            "login": {
                                "type": "boolean",
                                "description": "是否以登录 shell 方式运行 bash/zsh（默认 true）",
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行指定工具

        Args:
            tool_name: 工具名称
            **kwargs: 工具参数（沙箱通过 ContextVar 注入，无需额外传递）

        Returns:
            工具执行结果

        Raises:
            ValueError: 工具不存在
        """
        if tool_name == "execute_shell":
            return await self._execute_shell(**kwargs)
        raise ValueError(f"未知工具: {tool_name}")

    def _execute_shell_desc(self) -> str:
        """execute_shell 工具描述"""
        return (
            "执行 shell 命令并返回输出。"
            "用于测试、构建、包管理器命令、git 命令和其他进程执行。"
            "优先使用 read_file 和工具 API 进行文件操作，而非 shell 命令。"
            "使用 -y 或 --yes 标志避免交互式提示。"
            "输出截断至 10,000 字符；超时默认为 60 秒。"
        )

    # ------------------------------------------------------------------
    # execute_shell 实现
    # ------------------------------------------------------------------

    async def _execute_shell(
        self,
        command: str | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
        **kwargs: Any,
    ) -> str:
        """执行 shell 命令（沙箱通过 ContextVar 注入）

        Args:
            command: 要执行的命令
            working_dir: 工作目录
            timeout: 超时秒数
            shell: shell 解释器
            login: 登录 shell 模式

        Returns:
            命令输出
        """
        if not command:
            return "错误：缺少命令参数。提供 command。"

        prepared = self._prepare_command(command, working_dir, timeout, shell, login)
        if isinstance(prepared, str):
            return prepared

        try:
            process = await self._spawn(
                prepared.command,
                prepared.cwd,
                prepared.env,
                prepared.shell_program,
                prepared.login,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=prepared.timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return f"错误：命令执行超时（{prepared.timeout} 秒）"
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            output_parts: list[str] = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\n退出码: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "（无输出）"

            max_len = self._clamp_int(timeout or self._MAX_OUTPUT, self._MAX_OUTPUT, 1000, self._MAX_OUTPUT)
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n...（{len(result) - max_len:,} 字符已截断）...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            return f"执行命令失败: {e}"

    # ------------------------------------------------------------------
    # 命令准备与安全守卫
    # ------------------------------------------------------------------

    def _prepare_command(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
    ) -> _PreparedCommand | str:
        """准备命令：解析工作目录、执行安全校验、构建环境变量

        沙箱通过 ContextVar 注入（L2 防线），restrict_to_workspace 为 L1 防线。

        Args:
            command: 命令字符串
            working_dir: 工作目录
            timeout: 超时
            shell: shell 名称或路径
            login: 是否登录 shell

        Returns:
            _PreparedCommand 或错误字符串
        """
        # 确定工作目录：优先使用 LLM 指定的 working_dir，
        # 未指定时使用 ContextVar 沙箱的 context_root（用户上下文），
        # 最后回退到 _workspace 或 CWD
        if working_dir:
            cwd = working_dir
        else:
            sandbox = _current_sandbox()
            if sandbox is not None:
                cwd = str(sandbox.context_root)
            elif self._workspace:
                cwd = self._workspace
            else:
                cwd = os.getcwd()

        # L2 防线：通过 ContextVar 获取当前任务沙箱校验（线程安全）
        sandbox_error = self._check_sandbox_path(cwd)
        if sandbox_error:
            return sandbox_error

        # L1 防线：当 restrict_to_workspace 启用时，
        # 阻止 LLM 提供的 working_dir 逃逸（防御降级）
        # 注意：如果已使用 ContextVar 沙箱（L2），L1 作为额外防护仍然生效
        if self.restrict_to_workspace and self._workspace:
            try:
                requested = Path(cwd).expanduser().resolve()
                workspace_root = Path(self._workspace).expanduser().resolve()
            except Exception:
                return (
                    "错误：working_dir 无法解析"
                    + _WORKSPACE_BOUNDARY_NOTE
                )
            if requested != workspace_root and workspace_root not in requested.parents:
                return (
                    "错误：working_dir 超出配置的工作区"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        # 进程级沙箱包裹：如果配置了 sandbox 后端，将原始命令嵌入沙箱命令
        command = self._wrap_sandbox(command, cwd)

        effective_timeout = self._resolve_timeout(timeout)
        env = self._build_env()

        shell_program, shell_error = self._resolve_shell(shell)
        if shell_error:
            return shell_error

        return _PreparedCommand(
            command=command,
            cwd=cwd,
            env=env,
            timeout=effective_timeout,
            shell_program=shell_program,
            login=True if login is None else login,
        )

    def _resolve_timeout(self, timeout: int | None) -> int | None:
        """解析有效超时时间（秒，None = 无限制）"""
        if timeout:
            return min(timeout, self._MAX_TIMEOUT)
        if self._default_timeout and self._default_timeout > 0:
            return self._default_timeout
        return None

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """安全守卫：阻止危险命令和内网 URL

        Args:
            command: 命令字符串
            cwd: 当前工作目录

        Returns:
            错误字符串或 None（安全）
        """
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in _DENY_PATTERNS:
            if re.search(pattern, lower):
                return "错误：命令被 deny 模式过滤器阻止"

        # SSRF 守卫：检测命令中的内网 URL（如 curl http://169.254.169.254/）
        if contains_internal_url(cmd):
            return (
                "错误：命令被 SSRF 守卫阻止（检测到内网 URL）"
                + _WORKSPACE_BOUNDARY_NOTE
            )

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return (
                    "错误：命令被安全守卫阻止（检测到路径遍历）"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    if self._is_benign_device_path(expanded):
                        continue
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                if self._is_benign_device_path(str(p)):
                    continue

                if (
                    p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                ):
                    return (
                        "错误：命令被安全守卫阻止（路径超出工作目录）"
                        + _WORKSPACE_BOUNDARY_NOTE
                    )

        return None

    @classmethod
    def _is_benign_device_path(cls, path: str) -> bool:
        """检查是否为内核设备文件路径"""
        if path in _BENIGN_DEVICE_PATHS:
            return True
        return path.startswith("/dev/fd/")

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        """提取命令中的绝对路径"""
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)", command)
        return posix_paths + home_paths

    # ------------------------------------------------------------------
    # 进程管理
    # ------------------------------------------------------------------

    @staticmethod
    async def _spawn(
        command: str,
        cwd: str,
        env: dict[str, str],
        shell_program: str | None = None,
        login: bool = True,
    ) -> asyncio.subprocess.Process:
        """在平台合适的 shell 中启动命令"""
        if _IS_WINDOWS:
            return await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        shell_program = shell_program or shutil.which("bash") or "/bin/bash"
        args = [shell_program]
        shell_name = Path(shell_program).name.lower()
        if login and shell_name in {"bash", "bash.exe", "zsh", "zsh.exe"}:
            args.append("-l")
        args.extend(["-c", command])
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """终止子进程并回收防止僵尸进程"""
        process.kill()
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError) as e:
                    logger.debug("进程已回收或未找到: {}", e)

    @staticmethod
    def _resolve_shell(shell: str | None) -> tuple[str | None, str | None]:
        """解析 shell 路径"""
        if not shell:
            return None, None
        if _IS_WINDOWS:
            return None, "错误：Windows 不支持 shell 参数"
        if "\0" in shell or "\n" in shell or "\r" in shell:
            return None, "错误：shell 包含无效字符"
        allowed = {"sh", "bash", "zsh"}
        path = Path(shell).expanduser()
        if path.is_absolute():
            if path.name not in allowed:
                return None, f"错误：不支持的 shell {shell!r}。仅支持 bash, sh, zsh"
            if not path.is_file() or not os.access(path, os.X_OK):
                return None, f"错误：shell 不可执行: {shell}"
            return str(path), None
        if "/" in shell or "\\" in shell:
            return None, "错误：shell 必须是 shell 名称或绝对路径"
        if shell not in allowed:
            return None, f"错误：不支持的 shell {shell!r}。仅支持 bash, sh, zsh"
        resolved = shutil.which(shell)
        if not resolved:
            return None, f"错误：未找到 shell: {shell}"
        return resolved, None

    # ------------------------------------------------------------------
    # 环境构建
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """构建最小化子进程环境

        Unix: 仅传递 HOME/LANG/TERM；bash -l 会加载用户 profile 提供 PATH 等。
        Windows: 传递一组关键系统变量。
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PYTHONUNBUFFERED": "1",
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            return env
        home = os.environ.get("HOME", "/tmp")
        env: dict[str, str] = {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONUNBUFFERED": "1",
        }
        return env

    def _check_sandbox_path(self, path: str) -> str | None:
        """检查路径是否在 ContextVar 沙箱内（L2 防线）

        通过 ContextVar 获取当前任务沙箱，实现线程安全。
        如果当前任务未绑定沙箱，则跳过 L2 校验。

        Args:
            path: 待校验的路径

        Returns:
            错误字符串或 None（安全）
        """
        sandbox = _current_sandbox()
        if sandbox is None:
            return None

        # 尝试调用 sandbox 的 resolve_safe 或 assert_allowed
        try:
            if hasattr(sandbox, "resolve_safe"):
                sandbox.resolve_safe(path)
            elif hasattr(sandbox, "assert_allowed"):
                sandbox.assert_allowed(path)
            else:
                # 如果 sandbox 没有已知方法，跳过 L2 校验
                logger.debug("请求级 sandbox 没有 resolve_safe 或 assert_allowed 方法")
                return None
        except Exception as e:
            logger.warning("L2 沙箱拦截: {}", e)
            return f"错误：沙箱拦截 - {e}" + _WORKSPACE_BOUNDARY_NOTE

        return None

    def _wrap_sandbox(self, command: str, cwd: str) -> str:
        """如果配置了进程级沙箱后端，将命令包裹在沙箱中执行。

        沙箱后端通过 plugins.tool_shell.sandbox 配置（nanobee.yaml）。
        当前支持：
        - "bwrap"：使用 bubblewrap 的 mount namespace 隔离
        - ""（空字符串）：不启用（默认）

        注意：沙箱后端的可用性取决于系统是否安装了对应命令。
        （例如 bwrap 需要 apt install bubblewrap）

        Args:
            command: 原始命令
            cwd: 当前工作目录（用作沙箱 workdir）

        Returns:
            包裹后的命令，或原始命令（未配置或不可用时）
        """
        sandbox_backend = self.get_config("sandbox", "")
        if not sandbox_backend:
            return command

        if _IS_WINDOWS:
            logger.warning(
                "沙箱 '{}' 在 Windows 上不可用；在无沙箱状态下运行",
                sandbox_backend,
            )
            return command

        try:
            # 使用框架定义的子进程工作区边界（ProcessWorkspace），
            # 而非沙箱 context_root，确保子进程仅暴露 workspace/ 目录
            from nanobee.kernel.context_sandbox_var import (
                current_bwrap_ro_bind,
                current_process_workspace,
            )
            process_workspace = current_process_workspace()
            ws = str(process_workspace) if process_workspace else cwd
            # 读取部署方通过 skills.enabled 推导的额外只读挂载路径
            extra_ro_bind = current_bwrap_ro_bind()
            wrapped = _wrap_sandbox_command(
                sandbox_backend, command, ws, cwd,
                extra_ro_bind=extra_ro_bind,
            )
            logger.info("命令已通过沙箱 '{}' 包裹 (ws={})", sandbox_backend, ws)
            return wrapped
        except (ValueError, RuntimeError) as e:
            logger.warning("沙箱 '{}' 不可用，在无沙箱状态下运行: {}", sandbox_backend, e)
            return command

    @staticmethod
    def _clamp_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
        """将值限制在 [minimum, maximum] 范围内"""
        if value is None:
            return default
        return max(minimum, min(value, maximum))
