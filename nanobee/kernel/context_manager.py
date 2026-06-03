"""上下文管理器 - 管理 Agent 的对话上下文"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConversationContext:
    """对话上下文

    每个上下文对应一个独立的对话会话，拥有独立的：
    - 消息历史（history.jsonl）
    - 记忆目录（memory/）
    - 工作目录（work/）
    """

    def __init__(self, context_id: str, base_dir: Path):
        """初始化上下文

        Args:
            context_id: 上下文唯一 ID
            base_dir: 上下文基础目录
        """
        self.context_id = context_id
        self.base_dir = base_dir
        self.work_dir = base_dir / "work"
        self.memory_dir = base_dir / "memory"
        self.history_file = base_dir / "history.jsonl"

        # 创建目录结构
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self._messages: list[dict[str, Any]] = []
        self._load_history()

    def _load_history(self) -> None:
        """从 history.jsonl 加载历史消息"""
        if not self.history_file.exists():
            return
        with open(self.history_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self._messages.append(json.loads(line))

    def add_message(self, role: str, content: str) -> None:
        """添加消息到历史

        Args:
            role: 角色（user / assistant / system）
            content: 消息内容
        """
        message = {"role": role, "content": content}
        self._messages.append(message)
        self._persist_message(message)

    def _persist_message(self, message: dict[str, Any]) -> None:
        """持久化消息到 history.jsonl"""
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def get_messages(self) -> list[dict[str, Any]]:
        """获取所有消息"""
        return self._messages.copy()

    def clear(self) -> None:
        """清空上下文（保留目录结构）"""
        self._messages.clear()
        if self.history_file.exists():
            self.history_file.unlink()
        logger.info(f"上下文 {self.context_id} 已清空")


class ContextManager:
    """上下文管理器

    负责管理多个对话上下文的创建、切换、销毁。
    """

    def __init__(self, kernel: Any):
        """初始化

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        self._contexts: dict[str, ConversationContext] = {}

        # 上下文基础目录
        work_dir = Path(kernel.config.get("work_dir", "."))
        self.contexts_base_dir = work_dir / "contexts"
        self.contexts_base_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create(self, context_id: str) -> ConversationContext:
        """获取或创建上下文

        Args:
            context_id: 上下文 ID

        Returns:
            对话上下文实例
        """
        if context_id not in self._contexts:
            base_dir = self.contexts_base_dir / context_id
            self._contexts[context_id] = ConversationContext(context_id, base_dir)
            logger.info(f"创建上下文: {context_id}（目录: {base_dir}）")

        return self._contexts[context_id]

    async def switch(self, context_id: str) -> ConversationContext:
        """切换到指定上下文（别名：get_or_create）

        Args:
            context_id: 上下文 ID

        Returns:
            对话上下文实例
        """
        return await self.get_or_create(context_id)

    async def remove(self, context_id: str) -> bool:
        """移除上下文（同时删除目录）

        Args:
            context_id: 上下文 ID

        Returns:
            是否移除成功
        """
        if context_id not in self._contexts:
            return False

        ctx = self._contexts.pop(context_id)

        # 安全检查：只允许删除 contexts_base_dir 下的子目录
        base_dir = ctx.base_dir.resolve()
        allowed = self.contexts_base_dir.resolve()
        if not str(base_dir).startswith(str(allowed) + "/") and base_dir != allowed:
            logger.error(
                "安全拦截：base_dir %s 不在允许的 %s 下",
                base_dir, allowed,
            )
            return False

        import shutil
        if base_dir.exists():
            shutil.rmtree(base_dir)
        logger.info(f"移除上下文: {context_id}")
        return True

    def list_contexts(self) -> list[str]:
        """列出所有上下文 ID"""
        return list(self._contexts.keys())
