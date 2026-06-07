"""统一的日志模块（基于 loguru）。

所有模块应该通过 ``from nanobee.utils.logger import logger`` 导入。
"""

from __future__ import annotations

from loguru import logger

__all__ = ["logger"]
