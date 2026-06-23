"""
SessionManager — Session 生命周期管理。

缓存层：内存缓存避免重复磁盘 I/O。
业务层：CRUD、fork（复制会话）、list（高性能枚举）。
迁移层：自动将旧版 .history/default.jsonl 迁移到 sessions/。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from nanobee.session.session import Session
from nanobee.session.session_store import SessionStore
from nanobee.utils.logger import logger


class SessionManager:
    """Session 生命周期管理。

    职责：
    - 内存缓存（避免每轮重复磁盘 I/O）
    - CRUD 操作（创建、读取、删除）
    - fork 复制（用于"从此处新开会话"）
    - 高性能列表（仅读元数据行）
    - 优雅退出时 flush 所有缓存
    - 旧版历史文件迁移

    Attributes:
        store: SessionStore 实例（文件 I/O 层）。
    """

    def __init__(self, sessions_base_dir: str | Path) -> None:
        """初始化 SessionManager。

        Args:
            sessions_base_dir: sessions 存储根目录（通常为 <data_dir>/users/）。
        """
        self.store = SessionStore(sessions_base_dir)
        # 缓存：(user_id, session_id) → Session
        self._cache: dict[tuple[str, str], Session] = {}

    # ---- 核心 CRUD ----

    def get_or_create(self, user_id: str, session_id: str = "default") -> Session:
        """获取或创建 Session。

        优先从内存缓存读取，缓存未命中时从磁盘加载。
        磁盘也不存在时创建新的 Session。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID（格式 channel:chat_id，默认 "default"）。

        Returns:
            Session 实例。
        """
        key = (user_id, session_id)

        # 缓存命中
        if key in self._cache:
            return self._cache[key]

        # 磁盘加载
        session = self.store.load(user_id, session_id)
        if session is not None:
            self._cache[key] = session
            return session

        # 创建新 session
        session = Session(
            session_id=session_id,
            user_id=user_id,
        )
        self._cache[key] = session
        logger.debug("创建 session: user={} session={}", user_id, session_id)
        return session

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """保存 Session 到磁盘并更新缓存。

        Args:
            session: Session 实例。
            fsync: 是否同步刷盘（优雅退出时使用）。
        """
        key = (session.user_id, session.session_id)
        self._cache[key] = session
        self.store.save(session, fsync=fsync)

    def delete(self, user_id: str, session_id: str) -> bool:
        """删除 Session（文件 + 缓存）。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            是否成功删除。
        """
        key = (user_id, session_id)
        self._cache.pop(key, None)
        return self.store.delete(user_id, session_id)

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """列出某用户的所有 session 摘要信息。

        仅读元数据行，不加载全量消息。缓存中已有的 session 优先返回缓存数据。

        Args:
            user_id: 用户 ID。

        Returns:
            session 摘要列表。
        """
        # 从 store 获取文件中的 session 列表
        results = self.store.list_sessions(user_id)

        # 合并缓存中的 session（可能比磁盘更新）
        seen = {s.get("session_id") for s in results if s.get("session_id")}
        for (uid, sid), session in self._cache.items():
            if uid == user_id and sid not in seen:
                results.append(session.to_metadata_dict())

        return results

    def invalidate(self, user_id: str, session_id: str) -> None:
        """从缓存中移除指定 session。

        用于重新加载或自动压缩后刷新缓存。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
        """
        key = (user_id, session_id)
        self._cache.pop(key, None)

    # ---- Consolidation ----

    def consolidate(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        keep_last_n: int = 8,
    ) -> dict[str, Any]:
        """执行历史压缩：归档摘要 → 裁剪消息 → 注入摘要。

        将保留范围之外的消息归档到 .consolidation.jsonl，
        裁剪 session 到 keep_last_n 条，并在消息列表开头注入
        system 消息（含历史摘要），供后续 LLM 上下文参考。

        这是纯机制：摘要质量由 LLM 负责，框架只做 I/O 和裁剪。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            summary: LLM 生成的摘要文本（不含角色前缀）。
            keep_last_n: 保留的最近消息条数（默认 8）。

        Returns:
            操作结果字典：{"archived_count": int, "archived_index": int,
            "before_count": int, "after_count": int}

        Raises:
            ValueError: 参数无效时。
        """
        if keep_last_n < 1:
            raise ValueError(f"keep_last_n 必须 ≥ 1，当前为 {keep_last_n}")

        session = self.get_or_create(user_id, session_id)
        before_count = len(session.messages)

        # 裁剪行数不足时无需压缩
        if before_count <= keep_last_n:
            logger.debug(
                "无需压缩: session={} 仅 {} 条消息 (keep_last_n={})",
                session_id, before_count, keep_last_n,
            )
            return {
                "archived_count": 0,
                "archived_index": -1,
                "before_count": before_count,
                "after_count": before_count,
            }

        # 提取待归档的消息
        archived_messages = session.messages[: -keep_last_n]
        archived_count = len(archived_messages)

        # 摘要为空时降级为 raw archive（保留前 500 字符）
        safe_summary = summary.strip() if summary else ""
        if not safe_summary:
            raw_preview = " ".join(
                str(m.get("content", ""))[:500] for m in archived_messages[:20]
            )
            safe_summary = f"[raw archive] {raw_preview}"
            logger.warning(
                "LLM 摘要为空，降级为 raw archive: session={}", session_id,
            )

        # 写入压缩归档
        archived_index = self.store.append_consolidation(
            user_id, session_id, safe_summary, archived_count,
        )

        # 裁剪到 keep_last_n 条
        session.trim_to_last_n(keep_last_n)

        # 更新 last_consolidated 累积计数
        session.last_consolidated += archived_count

        # 在消息列表开头注入摘要 system 消息
        # 注意：摘要注入在裁剪之后，所以是第一条消息
        system_msg = {
            "role": "system",
            "content": f"[历史摘要 #{archived_index}] 以下对话的早期部分摘要（"
                       f"共 {archived_count} 条消息已归档）：\n{safe_summary}",
        }
        session.messages.insert(0, system_msg)
        session.updated_at = datetime.now()

        # 持久化
        self.save(session)
        after_count = len(session.messages)

        logger.info(
            "consolidate: session={} 压缩 {} → {} 条 (归档 #{} 共 {} 条)",
            session_id, before_count, after_count, archived_index, archived_count,
        )

        return {
            "archived_count": archived_count,
            "archived_index": archived_index,
            "before_count": before_count,
            "after_count": after_count,
        }

    # ---- Fork ----

    def fork(
        self,
        source: Session,
        target_session_id: str,
        before_message_index: int | None = None,
    ) -> Session | None:
        """复制一个 session 到新 ID。

        用于"从这条消息开始新对话"场景。

        Args:
            source: 源 Session。
            target_session_id: 目标 session ID。
            before_message_index: 截断点索引（仅保留该索引之前的消息）。
                None 时复制全部消息。

        Returns:
            新的 Session 实例，源 session 不匹配时返回 None。
        """
        # 确定消息范围
        messages = list(source.messages)
        if before_message_index is not None:
            if before_message_index < 0 or before_message_index > len(messages):
                logger.warning(
                    "fork 索引越界: {} (messages={})",
                    before_message_index, len(messages),
                )
                return None
            messages = messages[:before_message_index]

        new_session = Session(
            session_id=target_session_id,
            user_id=source.user_id,
            messages=messages,
            metadata={"forked_from": source.session_id, "forked_at": datetime.now().isoformat()},
        )
        # 保存目标 session
        self.save(new_session)
        logger.info(
            "fork session: {} → {} ({} messages)",
            source.session_id, target_session_id, len(messages),
        )
        return new_session

    # ---- Flush ----

    def flush_all(self) -> int:
        """优雅退出时将所有缓存的 session 刷入磁盘。

        Returns:
            实际 flush 的 session 数量。
        """
        count = 0
        for (user_id, session_id), session in list(self._cache.items()):
            try:
                self.store.save(session, fsync=True)
                count += 1
            except Exception:
                logger.exception("flush session 失败: user={} session={}", user_id, session_id)
        self._cache.clear()
        if count > 0:
            logger.info("flush {} session(s) 到磁盘", count)
        return count



__all__ = [
    "SessionManager",
]
