"""
memory_echo 参考插件 —— 读取 memory.txt 原样注入记忆段

Phase 3 参考插件：不实现 MemoryPlugin 抽象接口（保持极简），
仅通过 Hook 机制从文件读取内容注入 System Prompt。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobee.plugins.base import NanobeePlugin

logger = logging.getLogger(__name__)


class MemoryEchoPlugin(NanobeePlugin):
    """极简记忆插件：将 memory.txt 内容原样注入 System Prompt 的记忆段。

    Plugin type 设为 ``"echo"``，通过 ``stage = "记忆"`` 控制注入到
    ``## 记忆`` 段。``contribute_to_prompt`` 每次调用都重新读取文件，
    无状态缓存，确保文件内容变化即时生效。
    """

    name = "memory_echo"
    version = "1.0.0"
    plugin_type = "echo"
    stage = "记忆"

    def contribute_to_prompt(self, context: Any) -> str | None:
        """读取 {context.base_dir}/memory.txt，原样返回。

        Args:
            context: UserContext 实例，需包含 base_dir 属性

        Returns:
            文件内容（strip 后），文件不存在或读取失败时返回 None
        """
        try:
            base_dir = getattr(context, "base_dir", None)
            if base_dir is None:
                return None
            memory_file = Path(base_dir) / "memory.txt"
            if not memory_file.exists():
                return None
            content = memory_file.read_text(encoding="utf-8").strip()
            return content if content else None
        except Exception:
            logger.exception("memory_echo 读取 memory.txt 失败")
            return None
