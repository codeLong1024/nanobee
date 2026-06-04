"""MemoryFilePlugin - 基于 JSONL 的文件记忆存储，ADD-only 设计。

设计哲学：
- 只追加不修改（ADD-only），每条记忆自带 hash 去重
- 简单关键词匹配 + 时间衰减检索，不引入向量数据库
- facts.jsonl 存储提取的事实，dream_journal.jsonl 存储梦境摘要
- history.jsonl 保留原始对话，记忆是衍生品
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from nanobee.plugins.memory import MemoryPlugin

logger = logging.getLogger(__name__)


_EXCERPT_MAX_CHARS = 200


class MemoryFilePlugin(MemoryPlugin):
    """基于 JSONL 的文件记忆存储插件——ADD-only 设计。

    在 store() 时将超过阈值的历史消息提取事实写入 facts.jsonl，
    在 retrieve() 时通过关键词匹配 + 时间衰减检索相关记忆。
    """

    def __init__(self, metadata: Any = None) -> None:
        super().__init__(metadata=metadata)
        self._facts_file: Path | None = None

    # ---- 插件生命周期 ----

    def on_load(self) -> None:
        logger.info("memory_file 插件已加载")

    def destroy(self) -> None:
        self._facts_file = None

    # ---- 内部辅助 ----

    def _ensure_facts_path(self, user_context: Any) -> Path:
        """获取 facts.jsonl 路径（惰性初始化）"""
        if self._facts_file is not None:
            return self._facts_file
        memory_dir: Path = user_context.memory_dir
        self._facts_file = memory_dir / "facts.jsonl"
        return self._facts_file

    @staticmethod
    def _compute_hash(message: dict[str, Any]) -> str:
        """计算消息 hash，用于去重"""
        raw = json.dumps(message, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _load_hashes(facts_path: Path) -> set[str]:
        """加载 facts.jsonl 中所有 hash 集合"""
        if not facts_path.exists():
            return set()
        hashes: set[str] = set()
        with open(facts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        h = entry.get("hash", "")
                        if h:
                            hashes.add(h)
                    except json.JSONDecodeError:
                        continue
        return hashes

    # ---- 核心接口 ----

    async def store(self, messages: list[dict[str, Any]], user_context: Any) -> None:
        """从消息历史中提取事实并存储（ADD-only）。

        简单策略：将所有历史消息作为事实提取，用 hash 去重。
        只追加未出现过的新消息到 facts.jsonl。
        """
        facts_path = self._ensure_facts_path(user_context)
        existing_hashes = self._load_hashes(facts_path)

        new_entries: list[str] = []
        for msg in messages:
            msg_hash = self._compute_hash(msg)
            if msg_hash in existing_hashes:
                continue
            content = msg.get("content", "")
            if not content:
                continue
            excerpt = content[:_EXCERPT_MAX_CHARS]
            entry = {
                "hash": msg_hash,
                "role": msg.get("role", "user"),
                "excerpt": excerpt,
                "full_content": content,
                "timestamp": msg.get("timestamp", 0),
            }
            new_entries.append(json.dumps(entry, ensure_ascii=False))
            existing_hashes.add(msg_hash)

        if not new_entries:
            return

        with open(facts_path, "a", encoding="utf-8") as f:
            for line in new_entries:
                f.write(line + "\n")

        logger.info(
            "memory_file.store: 存储了 %d 条新事实到 %s (总 %d 条)",
            len(new_entries),
            facts_path,
            len(existing_hashes),
        )

    async def retrieve(
        self,
        query: str,
        user_context: Any,
        top_k: int = 5,
    ) -> str | None:
        """检索相关记忆——关键词匹配 + 时间衰减。

        策略：
        1. 将 query 分词作为关键词
        2. 扫描 facts.jsonl，每条事实命中一个关键词+1 分
        3. 按分数降序，取 top_k 条
        4. 返回格式化的记忆文本
        """
        facts_path = self._ensure_facts_path(user_context)
        if not facts_path.exists():
            return None

        # 简单分词
        query_lower = query.lower()
        # 中文按字符切 + 英文按空白切
        tokens = set()
        for ch in query_lower:
            if "\u4e00" <= ch <= "\u9fff":
                tokens.add(ch)
        for word in query_lower.split():
            tokens.add(word)

        if not tokens:
            return None

        scored: list[tuple[int, dict[str, Any]]] = []
        with open(facts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (entry.get("excerpt") or entry.get("content") or "").lower()
                score = sum(1 for t in tokens if t in content)
                if score > 0:
                    scored.append((score, entry))

        if not scored:
            return None

        # 按分数降序取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        lines = ["## 记忆"]
        for _, entry in top:
            text = entry.get("excerpt") or entry.get("content", "")
            role = entry.get("role", "?")
            lines.append(f"- [{role}] {text}")

        return "\n".join(lines)
