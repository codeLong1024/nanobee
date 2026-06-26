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
from nanobee.kernel.context_sandbox_var import (
    current_bwrap_ro_bind,
    current_process_workspace,
    current_sandbox as _current_sandbox,
)
from nanobee.plugins import ToolPlugin
from nanobee.security.network import contains_internal_url

from nanobee.utils.logger import logger


_IS_WINDOWS = sys.platform == "win32"



# 安装/卸载类 deny 模式 — 沙箱环境应保持洁净，禁止运行时变更依赖
_INSTALL_DENY_PATTERNS: list[str] = [
    r"\bpip[23]?\s+install\b",
    r"\bpython3?\s+-m\s+pip\s+install\b",
    r"\bnpm\s+(install|i|add)\b",
    r"\byarn\s+(add|install)\b",
    r"\bpnpm\s+(install|add)\b",
    r"\bconda\s+install\b",
    r"\bapt-get\s+install\b",
    r"\bapt\s+install\b",
    r"\bbrew\s+install\b",
]

# 危险系统操作 deny 模式
_DANGER_DENY_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"(?:^|[;&|]\s*)format(?!=)\b",
    r"\b(mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
]




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
    """Shell 工具插件 — 提供 execute_shell 和 write_stdin 工具"""

    name = "tool_shell"
    version = "1.0.0"
    plugin_type = "tool"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    def __init__(
        self,
        metadata: Any = None,
    ):
        super().__init__(metadata)
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
                                "description": (
                                    "命令的工作目录（可选，不提供时默认可写工作目录）。"
                                    "不要用 cd 切换目录。"
                                    "沙箱仅允许在此目录及其子目录中写入文件。"
                                ),
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
            "注意：运行时安装或卸载软件包（pip/npm/apt/brew 等）已被安全策略禁止。"
            "缺少模块时，请告知用户联系管理员安装。"
            "【沙箱约束】命令在隔离沙箱中运行，文件写入必须使用相对路径。"
            "绝对路径（如 --dump /home/xxx/reports）指向的目录在沙箱中不存在。"
            "所有文件输出到 CWD 或其子目录，如 --dump ./reports。"
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

            output_parts: list[str] = [f"[CWD: {prepared.cwd}]"]

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
                # 末尾优先：保留头部 20% 和尾部 80%，确保错误信息不被截断
                head_len = max_len // 5
                tail_len = max_len - head_len
                result = (
                    result[:head_len]
                    + f"\n\n...（{len(result) - max_len:,} 字符已截断，保留头部 {head_len} + 尾部 {tail_len}）...\n\n"
                    + result[-tail_len:]
                )

            return result

        except Exception as e:
            return f"执行命令失败: {e}"

    # ------------------------------------------------------------------
    # 命令准备与安全守卫
    # ------------------------------------------------------------------

    def _resolve_and_validate_working_dir(
        self, working_dir: str | None
    ) -> tuple[str, str | None]:
        """解析并校验工作目录（统一入口，单点决策）。

        优先级：
        1. working_dir 显式传入 → 校验边界后使用
        2. 回退到 process_workspace（必须有）

        Args:
            working_dir: 用户指定的工作目录（可选）

        Returns:
            (resolved_cwd, error_message)
            - 成功: (path_str, None)
            - 失败: ("", error_str)
        """
        if working_dir:
            return self._validate_explicit_cwd(working_dir)

        # 默认值：process_workspace
        process_ws = current_process_workspace()
        if process_ws is None:
            return "", "错误：未设置 process_workspace，无法确定工作目录"
        return str(process_ws), None

    def _validate_explicit_cwd(self, cwd: str) -> tuple[str, str | None]:
        """校验显式传入的 working_dir

        Args:
            cwd: 用户指定的工作目录路径

        Returns:
            (resolved_cwd, error_message)
            - 成功: (path_str, None)
            - 失败: ("", error_str)
        """
        try:
            requested = Path(cwd).expanduser().resolve()
        except Exception:
            return "", "错误：working_dir 无法解析为有效路径"

        # L1：process_workspace 边界校验
        process_ws = current_process_workspace()
        if process_ws is not None:
            ws_root = process_ws.resolve()
            if requested != ws_root and ws_root not in requested.parents:
                return "", (
                    "错误：working_dir 超出可写工作目录\n"
                    f"  你指定的路径: {requested}\n"
                    f"  可写工作目录: {ws_root}\n"
                    "  请将 working_dir 设为工作目录或其子目录，"
                    "或不传 working_dir 使用默认值"
                )

        # L2：ContextVar 沙箱校验
        sandbox = _current_sandbox()
        if sandbox is not None:
            try:
                sandbox.resolve_safe(cwd)
            except Exception as e:
                logger.warning("L2 沙箱拦截: {}", e)
                return "", f"错误：沙箱拦截 - {e}"

        return str(requested), None

    def _prepare_command(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
    ) -> _PreparedCommand | str:
        """准备命令：解析工作目录、执行安全校验、构建环境变量

        Args:
            command: 命令字符串
            working_dir: 工作目录
            timeout: 超时
            shell: shell 名称或路径
            login: 是否登录 shell

        Returns:
            _PreparedCommand 或错误字符串
        """
        # 统一入口：解析并校验工作目录
        cwd, error = self._resolve_and_validate_working_dir(working_dir)
        if error:
            return error

        guard_error = self._guard_command(command)
        if guard_error:
            return guard_error

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

    def _guard_command(self, command: str) -> str | None:
        """安全守卫：阻止危险命令和内网 URL

        Args:
            command: 命令字符串

        Returns:
            错误字符串或 None（安全）
        """
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in _INSTALL_DENY_PATTERNS:
            if re.search(pattern, lower):
                return "运行时安装或卸载软件包已被安全策略禁止。缺失依赖请联系管理员处理。"

        for pattern in _DANGER_DENY_PATTERNS:
            if re.search(pattern, lower):
                return "危险系统操作（rm -rf / dd / shutdown 等）已被安全策略禁止。"

        # SSRF 守卫：检测命令中的内网 URL（如 curl http://169.254.169.254/）
        if contains_internal_url(cmd):
            return "错误：命令被 SSRF 守卫阻止（检测到内网 URL）"

        return None

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
        """构建最小化子进程环境"""
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

    def _wrap_sandbox(self, command: str, cwd: str) -> str:
        """如果配置了进程级沙箱后端，将命令包裹在沙箱中执行。"""
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
            process_workspace = current_process_workspace()
            ws = str(process_workspace) if process_workspace else cwd
            extra_ro_bind = list(current_bwrap_ro_bind() or [])

            # 自动从 ContextSandbox 获取只读根目录（如内置/实例技能目录），
            # 映射为 bwrap 只读挂载，让 LLM 的 execute_shell 脚本可用
            sandbox_ctx = _current_sandbox()
            if sandbox_ctx:
                for ro_root in sandbox_ctx.read_only_roots:
                    extra_ro_bind.append(str(ro_root))

            original_command = command

            # 配置声明的额外只读挂载（source:target 格式，如 venv 或 SDK 目录）
            # 由部署方在 nanobee.yaml plugins.tool_shell.extra_mounts 中指定
            extra_mounts = self.get_config("extra_mounts", [])
            if isinstance(extra_mounts, list) and extra_mounts:
                extra_ro_bind.extend(extra_mounts)
                logger.info("沙箱额外挂载: {}", extra_mounts)

            # 配置声明的环境变量注入（部署方通过 plugins.tool_shell.env 指定）
            # 注意：必须用 export 而非 KEY=val cmd 前置，否则 env var 只作用于第一个命令
            # （如 "PYTHONPATH=/x cd dir && python3 ..." 中 python3 拿不到 PYTHONPATH）
            env_overrides = self.get_config("env", {})
            if isinstance(env_overrides, dict) and env_overrides:
                set_env = "; ".join(
                    f"export {k}={v}" for k, v in env_overrides.items()
                )
                command = f"{set_env}; {command}"
                logger.info("沙箱环境变量注入: {}", env_overrides)

            wrapped = _wrap_sandbox_command(
                sandbox_backend, command, ws, cwd,
                extra_ro_bind=extra_ro_bind,
            )
            logger.info("命令已通过沙箱 '{}' 包裹 (ws={})", sandbox_backend, ws)
            return wrapped
        except (ValueError, RuntimeError) as e:
            logger.warning("沙箱 '{}' 不可用，在无沙箱状态下运行: {}", sandbox_backend, e)
            return original_command

    @staticmethod
    def _clamp_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
        """将值限制在 [minimum, maximum] 范围内"""
        if value is None:
            return default
        return max(minimum, min(value, maximum))
