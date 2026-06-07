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
from pathlib import Path
from typing import Any

from nanobee.plugins.memory import MemoryPlugin

from nanobee.utils.logger import logger



_EXCERPT_MAX_CHARS = 200
_MAX_FACTS = 1000  # 存储上限，超过后触发压缩
_SCORE_RETENTION = 500  # 压缩后保留条数
_STORE_THRESHOLD = 20  # 对话达到此长度时触发记忆存储


class MemoryFilePlugin(MemoryPlugin):
    """基于 JSONL 的文件记忆存储插件——ADD-only 设计。

    store() 提取消息历史中的事实写入 facts.jsonl，
    retrieve() 通过关键词匹配 + 时间衰减检索相关记忆，
    on_message_completed() 在对话轮次完成后异步触发 store。

    当 facts.jsonl 超过 _MAX_FACTS 条时自动压缩，按时间戳保留最新 _SCORE_RETENTION 条。
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

    @staticmethod
    def _load_all_entries(facts_path: Path) -> list[dict[str, Any]]:
        """加载 facts.jsonl 中所有条目"""
        if not facts_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with open(facts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    #  P0 — 容量管理

    async def _compact_facts(self, facts_path: Path) -> None:
        """压缩 facts.jsonl：按时间戳降序保留新近的 _SCORE_RETENTION 条。"""
        entries = self._load_all_entries(facts_path)
        if len(entries) <= _SCORE_RETENTION:
            return

        entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        kept = entries[:_SCORE_RETENTION]

        with open(facts_path, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        logger.info(
            "memory_file._compact_facts: 压缩完成，保留 %d 条 (原 %d 条)",
            len(kept),
            len(entries),
        )

    # ---- 核心接口 ----

    async def store(self, messages: list[dict[str, Any]], user_context: Any) -> None:
        """从消息历史中提取事实并存储（ADD-only）。

        简单策略：将所有历史消息作为事实提取，用 hash 去重。
        只追加未出现过的新消息到 facts.jsonl。
        写入后若总条数超过 _MAX_FACTS，触发自动压缩。
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

        new_total = len(existing_hashes)
        logger.info(
            "memory_file.store: 存储了 {} 条新事实到 {} (总 {} 条)",
            len(new_entries),
            facts_path,
            new_total,
        )

        # P0：总条数超过上限时触发压缩
        if new_total > _MAX_FACTS:
            await self._compact_facts(facts_path)

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

    # ---- P1: 异步 store（通过 on_message_completed Hook） ----

    async def on_message_completed(
        self,
        context: Any,
        messages: list[dict[str, Any]],
    ) -> None:
        """对话轮次完成后异步触发 store，不阻塞 AgentLoop。

        获取完整的对话历史，当达到 _STORE_THRESHOLD 时触发记忆存储。
        """
        if context is None:
            return

        try:
            full_messages = context.get_messages()
        except Exception:
            full_messages = messages

        if len(full_messages) < _STORE_THRESHOLD:
            return

        await self.store(full_messages, context)

    # ---- P1: 记忆注入 System Prompt ----

    def contribute_to_prompt(self, context: Any) -> str | None:
        """检索相关记忆并注入 System Prompt。

        以最近一条用户消息作为查询关键词，同步检索 facts.jsonl，
        匹配的记忆拼接到 ## 记忆 段。

        Args:
            context: 用户上下文（UserContext 实例）

        Returns:
            格式化的记忆文本，无匹配时返回 None
        """
        try:
            messages = context.get_messages()
        except Exception:
            return None

        # 取最近一条用户消息作为查询
        query = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                query = msg.get("content", "")
                break

        if not query:
            return None

        # 同步检索（不经过 async retrieve）
        try:
            facts_path = self._ensure_facts_path(context)
        except Exception:
            return None

        result = self._sync_retrieve(query, facts_path, top_k=5)
        return result

    def _sync_retrieve(self, query: str, facts_path: Path, top_k: int = 5) -> str | None:
        """同步版 retrieve，用于同步的 contribute_to_prompt。"""
        if not facts_path or not facts_path.exists():
            return None

        query_lower = query.lower()
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

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        lines = ["## 记忆"]
        for _, entry in top:
            text = entry.get("excerpt") or entry.get("content", "")
            role = entry.get("role", "?")
            lines.append(f"- [{role}] {text}")

        return "\n".join(lines)
