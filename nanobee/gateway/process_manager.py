"""跨平台进程控制模块。

提供子进程的启动、停止和存活检测功能。
POSIX 使用 os.kill + signal，Windows 使用 ctypes TerminateProcess。

遵循框架无知论：本模块只提供机制（进程控制），
不持有策略（何时启动、何时停止）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from loguru import logger


class ProcessManager:
    """跨平台进程生命周期管理器。

    负责启动 nanobee gateway 子进程、优雅终止（SIGTERM→SIGKILL）
    和进程存活检测。
    """

    # 轮询间隔（秒），用于 stop 时在 SIGTERM 和 SIGKILL 之间等待
    _STOP_POLL_INTERVAL = 0.2

    def start(
        self,
        config_path: Path,
        venv_path: Path,
        log_path: Path,
    ) -> subprocess.Popen:
        """启动 nanobee gateway 后台子进程。

        Args:
            config_path: nanobee 配置文件绝对路径。
            venv_path: 虚拟环境根目录（必须存在 bin/python）。
            log_path: 日志文件路径（stdout/stderr 重定向到此文件）。

        Returns:
            subprocess.Popen 对象，包含子进程 PID。

        Raises:
            FileNotFoundError: 当 venv 中的 Python 解释器不存在时。
            OSError: 当子进程启动失败时。
        """
        python_bin = venv_path / "bin" / "python"
        if not python_bin.exists():
            raise FileNotFoundError(f"Python interpreter not found: {python_bin}")

        cmd = [
            str(python_bin),
            "-m",
            "nanobee",
            "gateway",
            "-c",
            str(config_path),
        ]

        log_file = open(str(log_path), "a")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # 脱离父进程终端，自成进程组
            )
        except OSError:
            log_file.close()
            logger.exception("Failed to start gateway process")
            raise

        logger.info(
            "Gateway process started: pid={}, config={}",
            process.pid,
            config_path,
        )
        return process

    def stop(self, pid: int, timeout: float) -> None:
        """优雅终止指定进程。

        先发送 SIGTERM，在 timeout 秒内轮询等待退出。
        超时后发送 SIGKILL 强制终止。

        Args:
            pid: 要终止的进程 ID。
            timeout: SIGTERM 后等待的最大秒数。
        """
        if not self._is_process_running(pid):
            logger.info("Process {} already stopped", pid)
            return

        # 发送 SIGTERM
        self._send_signal(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to process {}", pid)

        # 轮询等待退出
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._is_process_running(pid):
                logger.info("Process {} terminated gracefully", pid)
                return
            time.sleep(self._STOP_POLL_INTERVAL)

        # 超时，发送 SIGKILL
        if self._is_process_running(pid):
            logger.warning(
                "Process {} did not exit after SIGTERM ({}s), sending SIGKILL",
                pid,
                timeout,
            )
            self._send_signal(pid, signal.SIGKILL)
            time.sleep(0.5)

    def is_running(self, pid: int) -> bool:
        """检测指定进程是否存活。

        使用 os.kill(pid, 0) 检测进程存在性（不发送信号）。

        Args:
            pid: 进程 ID。

        Returns:
            True 表示进程存在。
        """
        return self._is_process_running(pid)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _is_process_running(self, pid: int) -> bool:
        """内部：检测进程是否存活。"""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 无权访问但进程存在
            return True

    def _send_signal(self, pid: int, sig: int) -> None:
        """跨平台发送信号。

        POSIX 使用 os.kill，Windows 使用 TerminateProcess。
        """
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            logger.debug("Process {} already gone before signal {}", pid, sig)
        except PermissionError:
            logger.warning("Permission denied sending signal {} to pid {}", sig, pid)
            raise
