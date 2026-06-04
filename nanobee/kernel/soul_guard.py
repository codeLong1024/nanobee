"""灵魂守卫 - 守护 Agent 的人格和安全边界"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SoulViolationError(Exception):
    """灵魂文件被篡改异常"""
    pass


class SoulGuard:
    """灵魂守卫

    三层保护机制：
    - Layer 1：OS 级锁定（chmod 444 / chattr +i）
    - Layer 2：应用层拦截（Hook 拦截写入操作）
    - Layer 3：SHA-256 哈希校验（启动时校验，不一致则拒绝启动）
    """

    def __init__(self, kernel: Any):
        """初始化

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        self.core_md_path = Path(kernel.config.get("core_md_path", "core.md"))
        self._expected_hash: str | None = None

    async def check(self) -> None:
        """执行启动时校验

        Raises:
            SoulViolationError: 灵魂文件被篡改
            FileNotFoundError: 灵魂文件不存在
        """
        if not self.core_md_path.exists():
            # 自动创建默认 core.md
            logger.warning("灵魂文件不存在，正在创建默认文件: %s", self.core_md_path)
            from nanobee.kernel.core_parser import CoreMDParser
            CoreMDParser.create_default(self.core_md_path)

        # Layer 3：哈希校验
        current_hash = self._compute_hash()
        hash_file = self.core_md_path.with_suffix(self.core_md_path.suffix + ".sha256")

        if hash_file.exists():
            with open(hash_file, "r", encoding="utf-8") as hf:
                self._expected_hash = hf.read().strip()
        else:
            self._expected_hash = None

        if self._expected_hash is not None and current_hash != self._expected_hash:
            raise SoulViolationError(
                f"灵魂文件哈希校验失败！\n"
                f"期望: {self._expected_hash}\n"
                f"当前: {current_hash}\n"
                f"文件可能已被篡改: {self.core_md_path}"
            )

        # 持久化当前哈希
        self._expected_hash = current_hash
        with open(hash_file, "w", encoding="utf-8") as hf:
            hf.write(current_hash)
        logger.info("灵魂文件校验通过（哈希: %s...）", current_hash[:16])

        # Layer 1：设置文件权限
        self._set_readonly()

    def _compute_hash(self) -> str:
        """计算灵魂文件哈希"""
        with open(self.core_md_path, "r", encoding="utf-8") as f:
            content = f.read()
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _set_readonly(self) -> None:
        """Layer 1：设置灵魂文件为只读

        跨平台实现：
        - Unix: chmod 444
        - Windows: 设置文件属性为只读
        """
        try:
            if platform.system() != "Windows":
                # Unix: chmod 444
                os.chmod(self.core_md_path, 0o444)
                logger.info("已设置灵魂文件为只读（chmod 444）: %s", self.core_md_path)
            else:
                # Windows: 设置只读属性
                import stat
                os.chmod(self.core_md_path, stat.S_IREAD)
                logger.info("已设置灵魂文件为只读（Windows）: %s", self.core_md_path)
        except Exception as e:
            logger.warning("设置灵魂文件只读失败: %s", e)

    def is_core_md_write_attempt(self, path: str | Path) -> bool:
        """检查是否尝试写入灵魂文件

        Args:
            path: 写入目标路径

        Returns:
            是否是写入灵魂文件的尝试
        """
        path = Path(path).resolve()
        return path == self.core_md_path.resolve()

    async def intercept_write(self, path: str | Path, content: str) -> bool:
        """Layer 2：拦截写入操作

        Args:
            path: 写入目标路径
            content: 写入内容

        Returns:
            True 表示允许写入，False 表示拦截
        """
        if self.is_core_md_write_attempt(path):
            logger.error("拦截到对灵魂文件的写入尝试！路径: %s", path)
            # 发射灵魂 violation 事件
            await self.kernel.event_bus.publish("soul.violation", {
                "path": str(path),
                "content_preview": content[:100],
            })
            return False
        return True
