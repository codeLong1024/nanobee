"""上下文管理器 - 管理多租户用户上下文"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from nanobee.exceptions import ContextError
from nanobee.kernel.user_context import UserContext

from nanobee.utils.logger import logger
from nanobee.utils.user_id import is_safe_user_id


def _validate_user_id(user_id: str) -> str:
    """校验 user_id 合法性，非法值直接拒绝（不净化替换）。

    白名单判定收敛到 :mod:`nanobee.utils.user_id` 单一来源——出生点归一
    （``InboundMessage.context_id`` 属性 / ``_process_message`` key 派生）
    与本落点断言共用同一判定，防两处漂移。正常链路的 id 已在出生点完成
    归一（合法原样 / 非法哈希降级），本函数是直接调用方（测试、脚本、
    未来新入口）的安全网。

    拒绝而非净化：净化会产生别名碰撞（如 ``a/b`` 与 ``a_b`` 映射到同一
    目录），导致跨租户数据串写；fail-visible 优于静默写错位置。

    Args:
        user_id: 待校验的用户标识。

    Returns:
        校验通过时原样返回 user_id。

    Raises:
        ContextError: user_id 非字符串、为空、超过长度上界、含白名单外
            字符，或为 ``.`` / ``..``。
    """
    if not is_safe_user_id(user_id):
        raise ContextError(
            f"非法 user_id（仅允许 [A-Za-z0-9._-]，长度 1-64，"
            f"不得为 '.' 或 '..'）: {user_id!r}",
        )
    return user_id



class ContextManager:
    """上下文管理器

    负责管理多个用户上下文的创建、切换、销毁。
    每个用户拥有独立的 UserContext（目录隔离 + 元数据 + 历史）。
    用户数据存储在 work_dir/users/<user_id>/ 下。
    """

    def __init__(self, kernel: Any):
        """初始化

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        self._contexts: dict[str, UserContext] = {}

        # 用户基础目录（使用 kernel.data_dir，确保默认值一致）
        self.users_base_dir = Path(kernel.data_dir).expanduser() / "users"
        self.users_base_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create(self, user_id: str) -> UserContext:
        """获取或创建用户上下文

        创建时自动生成默认的 identity.yaml 元数据。
        不加载历史消息（懒加载），仅加载元数据。

        Args:
            user_id: 用户唯一标识。必须通过白名单校验（``[A-Za-z0-9._-]``，
                长度 1-64，不得为 ``.`` / ``..``）——本方法是全框架唯一的
                user_id 守卫点，所有以 user_id 拼接路径的下游（用户目录、
                审计文件、会话文件等）依赖本契约。

        Returns:
            用户上下文实例

        Raises:
            ContextError: user_id 未通过白名单校验。
        """
        _validate_user_id(user_id)
        if user_id not in self._contexts:
            base_dir = self.users_base_dir / user_id
            base_dir.mkdir(parents=True, exist_ok=True)
            ctx = UserContext(user_id, base_dir)
            ctx._ensure_identity_file()
            self._contexts[user_id] = ctx
            logger.info("创建用户上下文: {}（目录: {}）", user_id, base_dir)

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

        # 安全检查：只允许删除 users_base_dir 下的子目录
        base_dir = ctx.base_dir.resolve()
        allowed = self.users_base_dir.resolve()
        try:
            base_dir.relative_to(allowed)
            if base_dir == allowed:
                logger.error("安全拦截：不允许删除 users 根目录: {}", base_dir)
                return False
        except ValueError:
            logger.error(
                "安全拦截：base_dir %s 不在允许的 %s 下",
                base_dir, allowed,
            )
            return False

        if base_dir.exists():
            shutil.rmtree(base_dir)
        logger.info("移除用户上下文: {}", user_id)
        return True

    def list_contexts(self) -> list[str]:
        """列出所有用户上下文 ID"""
        return list(self._contexts.keys())
