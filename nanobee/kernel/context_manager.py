"""上下文管理器 - 管理多租户用户上下文"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from nanobee.kernel.user_context import ConversationContext, UserContext

logger = logging.getLogger(__name__)


class ContextManager:
    """上下文管理器

    负责管理多个用户上下文的创建、切换、销毁。
    每个用户拥有独立的 UserContext（目录隔离 + 元数据 + 历史）。
    """

    def __init__(self, kernel: Any):
        """初始化

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        self._contexts: dict[str, UserContext] = {}

        # 上下文基础目录
        work_dir = Path(kernel.config.get("work_dir", "."))
        self.contexts_base_dir = work_dir / "contexts"
        self.contexts_base_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create(self, user_id: str) -> UserContext:
        """获取或创建用户上下文

        创建时自动生成默认的 context.yaml 元数据。
        不加载历史消息（懒加载），仅加载元数据。

        Args:
            user_id: 用户唯一标识

        Returns:
            用户上下文实例
        """
        if user_id not in self._contexts:
            base_dir = self.contexts_base_dir / user_id
            base_dir.mkdir(parents=True, exist_ok=True)
            ctx = UserContext(user_id, base_dir)
            ctx._ensure_meta_file()
            self._contexts[user_id] = ctx
            logger.info("创建用户上下文: %s（目录: %s）", user_id, base_dir)

        return self._contexts[user_id]

    async def get_metadata(self, user_id: str) -> dict[str, Any]:
        """仅获取用户元数据，不加载历史

        Args:
            user_id: 用户唯一标识

        Returns:
            元数据字典（不包含历史消息）
        """
        ctx = await self.get_or_create(user_id)
        return ctx.metadata.to_dict()

    async def switch(self, user_id: str) -> UserContext:
        """切换到指定用户上下文

        Args:
            user_id: 用户标识

        Returns:
            用户上下文实例
        """
        return await self.get_or_create(user_id)

    async def remove(self, user_id: str) -> bool:
        """移除用户上下文（同时删除目录）

        Args:
            user_id: 用户标识

        Returns:
            是否移除成功
        """
        if user_id not in self._contexts:
            return False

        ctx = self._contexts.pop(user_id)

        # 安全检查：只允许删除 contexts_base_dir 下的子目录
        base_dir = ctx.base_dir.resolve()
        allowed = self.contexts_base_dir.resolve()
        try:
            base_dir.relative_to(allowed)
            if base_dir == allowed:
                logger.error("安全拦截：不允许删除 contexts 根目录: %s", base_dir)
                return False
        except ValueError:
            logger.error(
                "安全拦截：base_dir %s 不在允许的 %s 下",
                base_dir, allowed,
            )
            return False

        if base_dir.exists():
            shutil.rmtree(base_dir)
        logger.info("移除用户上下文: %s", user_id)
        return True

    def list_contexts(self) -> list[str]:
        """列出所有用户上下文 ID"""
        return list(self._contexts.keys())
