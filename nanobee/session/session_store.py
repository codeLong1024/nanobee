"""
SessionStore — Session 的文件存储层。

纯 I/O，无缓存：读取时从文件加载，写入时原子替换。
上层 SessionManager 负责缓存和业务编排。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from nanobee.session.session import Session
from nanobee.utils.logger import logger


class SessionStore:
    """Session 的文件存储层。

    Attributes:
        sessions_base_dir: sessions 存储的根目录（通常为 <data_dir>/users/）。
    """

    def __init__(self, sessions_base_dir: str | Path) -> None:
        """初始化 SessionStore。

        Args:
            sessions_base_dir: sessions 存储根目录（users/）。
        """
        self.sessions_base_dir = Path(sessions_base_dir).resolve()
        self.sessions_base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, user_id: str, session_id: str) -> Path:
        """返回 session 文件的完整路径。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID（含冒号）。

        Returns:
            文件的绝对路径。
        """
        safe = session_id.replace(":", "__")
        return self.sessions_base_dir / user_id / "sessions" / f"{safe}.jsonl"

    def _consolidation_path(self, user_id: str, session_id: str) -> Path:
        """返回 consolidation 归档文件的完整路径。

        与 session .jsonl 同目录，后缀为 .consolidation.jsonl。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID（含冒号）。

        Returns:
            归档文件的绝对路径。
        """
        safe = session_id.replace(":", "__")
        return self.sessions_base_dir / user_id / "sessions" / f"{safe}.consolidation.jsonl"

    # ---- 归档 ----

    def append_consolidation(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        archived_count: int,
    ) -> int:
        """追加一条压缩归档记录到 .consolidation.jsonl 文件。

        每次调用追加一行 JSON，记录序号、摘要、归档消息数、时间戳。
        序号从已有行数自动推导（从 0 开始）。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。
            summary: LLM 生成的摘要文本。
            archived_count: 本次归档的消息条数。

        Returns:
            本次归档记录的序号（从 0 开始）。
        """
        from datetime import datetime

        path = self._consolidation_path(user_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 计算已有行数作为序号
        index = 0
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    index = sum(1 for _ in f)
            except OSError:
                logger.exception("读取 consolidation 归档文件失败: {}", path)

        record = {
            "index": index,
            "summary": summary,
            "archived_count": archived_count,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("追加 consolidation 归档记录失败: {}", path)
            raise

        logger.info(
            "consolidation 归档 #{}: session={} 归档 {} 条消息",
            index, session_id, archived_count,
        )
        return index

    # ---- 读取 ----

    def load(self, user_id: str, session_id: str) -> Session | None:
        """从 JSONL 文件加载 Session。

        文件格式：首行为 _type=metadata 的元数据行，后续为消息行。
        遇到损坏时自动尝试修复。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            Session 实例（含完整消息列表），文件不存在时返回 None。
        """
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return None

        lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [line.rstrip("\n") for line in f]
        except OSError:
            logger.exception("读取 session 文件失败: {}", path)
            return self._repair(user_id, session_id)

        if not lines:
            logger.warning("空 session 文件: {}", path)
            return None

        # 解析元数据行
        meta: dict[str, Any] = {}
        meta_line = lines[0]
        try:
            meta = json.loads(meta_line)
        except json.JSONDecodeError:
            logger.warning("session 元数据行损坏，尝试修复: {}", path)
            return self._repair(user_id, session_id)

        if meta.get("_type") != "metadata":
            logger.warning("session 文件首行非元数据，尝试修复: {}", path)
            return self._repair(user_id, session_id)

        session = Session.from_metadata_dict(user_id, meta)

        # 解析消息行
        messages: list[dict[str, Any]] = []
        for idx, line in enumerate(lines[1:], start=2):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if isinstance(msg, dict):
                    messages.append(msg)
            except json.JSONDecodeError:
                logger.warning("session 文件第 {} 行 JSON 损坏，跳过: {}", idx, path)

        session.messages = messages
        return session

    # ---- 写入 ----

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """原子地将 Session 写入 JSONL 文件。

        使用临时文件 + os.replace 确保写入原子性。
        在优雅退出时可传入 fsync=True 保证数据落盘。

        Args:
            session: 要保存的 Session 实例。
            fsync: 是否同步刷盘（优雅退出时使用）。
        """
        path = self._session_path(session.user_id, session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 写入临时文件后进行原子替换
        fd, tmp_path = tempfile.mkstemp(
            suffix=".jsonl.tmp",
            prefix=f"{session.session_id.replace(':', '_')}_",
            dir=path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # 首行：元数据
                f.write(json.dumps(session.to_metadata_dict(), ensure_ascii=False) + "\n")
                # 后续行：消息
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            os.replace(tmp_path, path)
        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---- 删除 ----

    def delete(self, user_id: str, session_id: str) -> bool:
        """删除 session 文件。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            是否成功删除。
        """
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return False
        path.unlink()
        logger.info("已删除 session 文件: {}", path)
        return True

    # ---- 列举 ----

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """列出某用户的所有 session 摘要信息。

        仅读取每文件的首行元数据，性能 O(n) 取决于 session 数而非消息数。

        Args:
            user_id: 用户 ID。

        Returns:
            session 摘要列表，每项包含 session_id、message_count、created_at 等。
        """
        sessions_dir = self.sessions_base_dir / user_id / "sessions"
        if not sessions_dir.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for fpath in sorted(sessions_dir.iterdir()):
            if not fpath.name.endswith(".jsonl"):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                meta = json.loads(first_line)
                if meta.get("_type") == "metadata":
                    # 追加文件名体现的 session_id（兼容元数据行缺失时的兜底）
                    meta.setdefault("session_id", fpath.stem.replace("__", ":"))
                    meta.pop("_type", None)
                    results.append(meta)
            except (OSError, json.JSONDecodeError):
                logger.debug("读取 session 元数据失败: {}", fpath)
                continue

        return results

    # ---- 修复 ----

    def _repair(self, user_id: str, session_id: str) -> Session | None:
        """修复损坏的 JSONL 文件：跳过无效行，尽量恢复有效数据。

        Args:
            user_id: 用户 ID。
            session_id: 会话 ID。

        Returns:
            修复后的 Session 实例，无法修复时返回 None。
        """
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return None

        logger.warning("正在修复 session 文件: {}", path)
        valid_lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                        valid_lines.append(line)
                    except json.JSONDecodeError:
                        pass  # 跳过损坏行
        except OSError:
            logger.exception("修复 session 文件时读取失败: {}", path)
            return None

        if not valid_lines:
            logger.error("session 文件完全损坏，无法修复: {}", path)
            path.unlink(missing_ok=True)
            return None

        # 如果修复后首行不是元数据，插入全新元数据行
        try:
            first = json.loads(valid_lines[0])
        except json.JSONDecodeError:
            first = {}

        if not isinstance(first, dict) or first.get("_type") != "metadata":
            logger.warning("修复 session 后首行非元数据，插入默认元数据")
            session = Session(session_id=session_id, user_id=user_id)
            valid_lines.insert(0, json.dumps(session.to_metadata_dict(), ensure_ascii=False))

        # 写出修复后的文件
        try:
            with open(path, "w", encoding="utf-8") as f:
                for line in valid_lines:
                    f.write(line + "\n")
        except OSError:
            logger.exception("写出修复后的 session 文件失败: {}", path)
            return None

        # 重新加载
        return self.load(user_id, session_id)


__all__ = [
    "SessionStore",
]
