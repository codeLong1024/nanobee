"""PID 文件原子操作模块。

提供 PID 文件的原子写入、读取、清理和列表功能。
通过 tmp+rename+fsync 三步确保 PID 文件完整性，
防止进程崩溃时留下不完整 PID 文件导致状态不一致。

遵循框架无知论：本模块只提供机制（PID 文件读写），
不持有策略（何时清理、何时检测）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from loguru import logger


class PidManager:
    """PID 文件原子操作管理器。

    所有 PID 文件以 <name>.pid 命名存储在 pid_dir 目录下。
    使用 tmp 文件 + os.rename + os.fsync 三步确保原子写入。
    """

    def __init__(self, pid_dir: Path) -> None:
        """初始化 PID 管理器。

        Args:
            pid_dir: PID 文件存放目录，不存在时自动创建。
        """
        self._pid_dir = pid_dir
        self._pid_dir.mkdir(parents=True, exist_ok=True)

    @property
    def pid_dir(self) -> Path:
        """PID 文件所在目录。"""
        return self._pid_dir

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def write(self, name: str, pid: int) -> None:
        """原子写入 PID 文件。

        先写入临时文件，再通过 os.rename（原子操作）移动，
        最后 fsync 强制落盘，确保不会留下不完整 PID 文件。

        Args:
            name: 实例名称。
            pid: 进程 ID。
        """
        pid_path = self._pid_path(name)
        # 在同一目录下创建临时文件，确保 rename 是原子操作（同文件系统）
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=f"{name}.", dir=self._pid_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(pid))
            os.rename(tmp_path, pid_path)
            # 确保元数据落盘
            self._fsync_dir()
        except OSError:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            logger.exception("Failed to write PID file for instance {}", name)
            raise

    def read(self, name: str) -> int | None:
        """读取 PID 文件内容。

        Args:
            name: 实例名称。

        Returns:
            进程 ID，文件不存在或内容无效时返回 None。
        """
        pid_path = self._pid_path(name)
        try:
            content = pid_path.read_text().strip()
        except FileNotFoundError:
            return None
        if not content:
            return None
        try:
            return int(content)
        except ValueError:
            logger.warning("Invalid PID content in {}: {}", pid_path, content)
            return None

    def remove(self, name: str) -> None:
        """删除 PID 文件。

        Args:
            name: 实例名称。
        """
        pid_path = self._pid_path(name)
        try:
            pid_path.unlink()
            self._fsync_dir()
        except FileNotFoundError:
            pass  # 幂等：不存在时不报错
        except OSError:
            logger.exception("Failed to remove PID file for instance {}", name)
            raise

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _pid_path(self, name: str) -> Path:
        """获取实例的 PID 文件路径。"""
        return self._pid_dir / f"{name}.pid"

    def _fsync_dir(self) -> None:
        """强制刷新目录元数据到磁盘。"""
        fd = os.open(self._pid_dir, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
