"""文件记忆插件实现"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nanobee.plugins.memory import MemoryPlugin

logger = logging.getLogger(__name__)


class MemoryFilePlugin(MemoryPlugin):
    """基于 jsonl 文件的记忆存储"""

    name = "memory_file"
    version = "1.0.0"

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._storage_path: Path | None = None

    def initialize(self, kernel: Any) -> None:
        """初始化记忆存储

        Args:
            kernel: NanobeeKernel 实例或包含配置的 dict
        """
        super().initialize(kernel)
        if self.kernel is None:
            return
        # 兼容 kernel 对象（有 .config 属性）和 dict 两种形式
        if isinstance(self.kernel, dict):
            work_dir = Path(self.kernel.get("work_dir", ".")).expanduser()
        else:
            work_dir = Path(getattr(self.kernel, "config", {}).get("work_dir", ".")).expanduser()
        self._storage_path = work_dir / "memories.jsonl"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("记忆存储已初始化: %s", self._storage_path)

    async def store(self, key: str, value: Any, memory_type: str = "default") -> None:
        """存储记忆"""
        entry = {"key": key, "value": value, "type": memory_type}
        if self._storage_path:
            with open(self._storage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def retrieve(self, key: str) -> Any | None:
        """检索记忆"""
        if not self._storage_path or not self._storage_path.exists():
            return None
        with open(self._storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if entry.get("key") == key:
                        return entry.get("value")
        return None

    async def search(self, query: str, limit: int = 10) -> list[Any]:
        """搜索记忆（按关键字匹配）"""
        results = []
        if not self._storage_path or not self._storage_path.exists():
            return results
        with open(self._storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if query.lower() in str(entry.get("value", "")).lower():
                        results.append(entry.get("value"))
                        if len(results) >= limit:
                            break
        return results

    async def delete(self, key: str) -> bool:
        """删除记忆"""
        deleted = False
        if not self._storage_path or not self._storage_path.exists():
            return False
        entries = []
        with open(self._storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if entry.get("key") == key:
                        deleted = True
                    else:
                        entries.append(entry)
        with open(self._storage_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if deleted:
            logger.info("已删除记忆: %s", key)
        return deleted

    async def list_all(self, memory_type: str | None = None) -> list[str]:
        """列出所有记忆键值"""
        if not self._storage_path or not self._storage_path.exists():
            return []
        keys = []
        with open(self._storage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if memory_type is None or entry.get("type") == memory_type:
                        keys.append(entry.get("key"))
        return keys
