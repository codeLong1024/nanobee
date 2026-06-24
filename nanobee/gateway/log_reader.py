"""日志读取模块。

提供日志文件的尾部读取和实时跟踪功能。
大文件使用反向读取避免全量加载。

遵循框架无知论：本模块只提供机制（文件读取），
不持有策略（读取多少行、何时轮换）。
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger


class LogReader:
    """日志读取器。

    支持 tail N 行和类似 tail -f 的实时流式跟踪。
    大文件反向读取避免一次性全量加载到内存。
    """

    # 每次读取的块大小（用于反向查找换行符）
    _CHUNK_SIZE = 4096

    def __init__(self) -> None:
        """初始化日志读取器。

        维护 _offsets 字典跟踪各文件的已读偏移量，
        用于 stream() 增量读取。
        """
        self._offsets: dict[str, int] = {}

    def tail(self, log_path: Path, lines: int = 50) -> str:
        """读取日志文件最后 N 行。

        使用反向 block 读取，避免大文件全量加载。
        空文件或不存在时返回空字符串。

        Args:
            log_path: 日志文件路径。
            lines: 需要读取的行数。

        Returns:
            最后 N 行的内容（字符串）。
        """
        try:
            return self._tail_reverse(log_path, lines)
        except FileNotFoundError:
            logger.debug("Log file not found: {}", log_path)
            return ""
        except OSError:
            logger.exception("Failed to read log file: {}", log_path)
            return ""

    def stream(self, log_path: Path) -> str | None:
        """读取日志文件新增内容（增量模式）。

        从上次读取的文件末尾开始，每次调用仅返回新增内容。
        没有新内容时返回 None。
        调用方可在循环中反复调用此方法模拟 tail -f。

        Args:
            log_path: 日志文件路径。

        Returns:
            新增的日志内容字符串，无新内容时返回 None。
        """
        try:
            return self._read_new_lines(log_path)
        except FileNotFoundError:
            return None
        except OSError:
            logger.exception("Failed to stream log file: {}", log_path)
            return None

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _tail_reverse(self, log_path: Path, lines: int) -> str:
        """反向读取文件最后 N 行。"""
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()

            if file_size == 0:
                return ""

            blocks: list[bytes] = []
            remaining = file_size
            found_lines = 0

            while remaining > 0 and found_lines <= lines:
                read_size = min(self._CHUNK_SIZE, remaining)
                remaining -= read_size
                f.seek(remaining)
                block = f.read(read_size)
                blocks.append(block)
                found_lines += block.count(b"\n")

            # 拼接从末尾开始的所有 block
            data = b"".join(reversed(blocks))

            # 截取最后 lines 行
            all_lines = data.split(b"\n")
            result_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

            return "\n".join(line.decode("utf-8", errors="replace") for line in result_lines)

    def _read_new_lines(self, log_path: Path) -> str | None:
        """读取文件新增内容（基于偏移量增量读取）。

        首次读取时从文件末尾开始（跳过已有内容），
        之后仅返回偏移量之后的新增内容。
        文件被截断时自动重置偏移量。
        """
        key = str(log_path)
        try:
            file_size = log_path.stat().st_size
        except FileNotFoundError:
            self._offsets.pop(key, None)
            return None

        prev_offset = self._offsets.get(key, 0)
        # 首次读取：从文件末尾开始，或文件被截断时重置
        if prev_offset == 0 or prev_offset > file_size:
            self._offsets[key] = file_size
            return None

        if prev_offset >= file_size:
            return None

        with open(log_path, "r") as f:
            f.seek(prev_offset)
            content = f.read()
            self._offsets[key] = f.tell()

        return content if content else None
