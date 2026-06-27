"""
用户上下文 — 多租户隔离的基本单元

每个用户一个目录，目录结构：
  users/{user_id}/
    identity.yaml  # 元数据（user_id, display_name, whitelist, blacklist）
    workspace/     # LLM 工作文件（工具写文件的目标目录）
    memory/        # 记忆目录
    skills/        # 技能目录
    sessions/      # 会话历史（SessionManager 管理）
    .tmp/          # 插件临时目录（框架创建，插件自管清理）

注意：沙箱根就是 base_dir（users/{user_id}/），
所有相对路径（memory/xxx、skills/xxx）都基于此解析。
identity.yaml 和 .tmp/ 受沙箱写保护，LLM 无法修改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from nanobee.utils.logger import logger


# identity.yaml 默认元数据
_USER_META_DEFAULTS: dict[str, Any] = {
    "user_id": "",
    "display_name": "",
    "whitelist": [],
    "blacklist": [],
}


class UserMetadata:
    """用户元数据，从 context.yaml 加载"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.user_id: str = str(data.get("user_id", ""))
        self.display_name: str = str(data.get("display_name", ""))
        self.whitelist: list[str] = list(data.get("whitelist", []))
        self.blacklist: list[str] = list(data.get("blacklist", []))

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "whitelist": list(self.whitelist),
            "blacklist": list(self.blacklist),
        }


class UserContext:
    """用户上下文 — 多租户隔离的基本单元

    每个 User 一个目录，封装了：
    - identity.yaml：元数据
    - workspace/：LLM 工作文件目录
    - memory/：记忆目录
    - .tmp/：插件临时目录

    历史管理已完全迁移到 SessionManager（sessions/ 目录），
    此类不再持有任何历史相关的方法。
    """

    def __init__(self, user_id: str, base_dir: Path) -> None:
        """初始化用户上下文

        Args:
            user_id: 用户唯一标识
            base_dir: 用户上下文根目录（users/<user_id>/）
        """
        self.user_id = user_id
        self.base_dir = base_dir.resolve()

        # identity.yaml 路径
        self.meta_file = self.base_dir / "identity.yaml"

        # 确保子目录存在
        self.base_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "memory").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "skills").mkdir(parents=True, exist_ok=True)
        (self.base_dir / ".tmp").mkdir(parents=True, exist_ok=True)

        # 元数据
        self._metadata: UserMetadata | None = None

    # ---- 兼容属性 ----

    @property
    def context_id(self) -> str:
        """兼容属性：等同于 user_id"""
        return self.user_id

    # ---- 元数据 ----

    @property
    def metadata(self) -> UserMetadata:
        """获取用户元数据（懒加载）"""
        if self._metadata is None:
            self._metadata = self._load_metadata()
        return self._metadata

    def reload_metadata(self) -> UserMetadata:
        """重新加载元数据（从文件）"""
        self._metadata = self._load_metadata()
        return self._metadata

    def _load_metadata(self) -> UserMetadata:
        """从 identity.yaml 加载元数据"""
        if not self.meta_file.exists():
            logger.warning("元数据文件不存在，使用默认值: {}", self.meta_file)
            return UserMetadata({"user_id": self.user_id})
        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}
            return UserMetadata(data)
        except Exception:
            logger.exception("加载元数据失败: {}", self.meta_file)
            return UserMetadata({"user_id": self.user_id})

    def _ensure_identity_file(self) -> None:
        """确保 identity.yaml 存在，不存在则创建默认"""
        if self.meta_file.exists():
            return
        default = dict(_USER_META_DEFAULTS)
        default["user_id"] = self.user_id
        with open(self.meta_file, "w", encoding="utf-8") as f:
            yaml.dump(default, f, allow_unicode=True, default_flow_style=False)
        logger.info("已创建默认元数据: {}", self.meta_file)

    # ---- 目录路径 ----

    @property
    def work_dir(self) -> Path:
        """LLM 工作目录 — 指向 workspace/ 子目录"""
        workspace_dir = self.base_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    @property
    def memory_dir(self) -> Path:
        """记忆目录"""
        return self.base_dir / "memory"

    @property
    def skills_dir(self) -> Path:
        """用户技能目录（可读可写，LLM 通过 execute_shell 创建技能时写入此处）"""
        return self.base_dir / "skills"

    @property
    def tmp_dir(self) -> Path:
        """插件临时目录"""
        return self.base_dir / ".tmp"

    # ---- 白/黑名单 ----

    @property
    def whitelist(self) -> list[str]:
        """白名单工具名列表"""
        return list(self.metadata.whitelist)

    @property
    def blacklist(self) -> list[str]:
        """黑名单工具名列表"""
        return list(self.metadata.blacklist)

    @property
    def context_root(self) -> Path:
        """上下文根目录（用于沙箱）— 指向 base_dir 自身"""
        return self.base_dir

    def __repr__(self) -> str:
        return f"UserContext(user_id={self.user_id!r}, base_dir={self.base_dir})"


__all__ = [
    "UserContext",
    "UserMetadata",
]
