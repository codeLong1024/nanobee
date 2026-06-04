"""
用户上下文 — 多租户隔离的基本单元

每个用户一个目录，目录结构：
  contexts/{user_id}/
    context.yaml    # 元数据（user_id, display_name, whitelist, blacklist）
    history.jsonl   # 对话历史
    memory/         # 记忆目录
    work/           # 工作目录
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# context.yaml 默认元数据
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


class ConversationContext:
    """对话上下文（UserContext 内部实现）

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
        self.skills_dir = base_dir / "skills"
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
        logger.info("上下文 %s 已清空", self.context_id)


class UserContext:
    """用户上下文 — 多租户隔离的基本单元

    每个 User 一个目录，封装了：
    - context.yaml：元数据（仅加载元数据，不加载历史）
    - ConversationContext：历史消息、记忆、工作目录
    """

    def __init__(self, user_id: str, base_dir: Path) -> None:
        """初始化用户上下文

        Args:
            user_id: 用户唯一标识
            base_dir: 用户上下文根目录
        """
        self.user_id = user_id
        self.base_dir = base_dir.resolve()

        # context.yaml 路径
        self.meta_file = self.base_dir / "context.yaml"

        # 内部 ConversationContext（历史消息、记忆、工作目录）
        self._conversation = ConversationContext(user_id, base_dir)

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
        """从 context.yaml 加载元数据"""
        if not self.meta_file.exists():
            logger.warning("元数据文件不存在，使用默认值: %s", self.meta_file)
            return UserMetadata({"user_id": self.user_id})
        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}
            return UserMetadata(data)
        except Exception:
            logger.exception("加载元数据失败: %s", self.meta_file)
            return UserMetadata({"user_id": self.user_id})

    def _ensure_meta_file(self) -> None:
        """确保 context.yaml 存在，不存在则创建默认"""
        if self.meta_file.exists():
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        default = dict(_USER_META_DEFAULTS)
        default["user_id"] = self.user_id
        with open(self.meta_file, "w", encoding="utf-8") as f:
            yaml.dump(default, f, allow_unicode=True, default_flow_style=False)
        logger.info("已创建默认元数据: %s", self.meta_file)

    # ---- 对话历史代理 ----

    @property
    def work_dir(self) -> Path:
        """工作目录"""
        return self._conversation.work_dir

    @property
    def memory_dir(self) -> Path:
        """记忆目录"""
        return self._conversation.memory_dir

    @property
    def history_file(self) -> Path:
        """历史文件路径"""
        return self._conversation.history_file

    def get_messages(self) -> list[dict[str, Any]]:
        """获取所有历史消息"""
        return self._conversation.get_messages()

    def add_message(self, role: str, content: str) -> None:
        """添加消息到历史"""
        self._conversation.add_message(role, content)

    def clear(self) -> None:
        """清空历史（保留目录结构）"""
        self._conversation.clear()

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
        """上下文根目录（用于沙箱）"""
        return self.base_dir

    def __repr__(self) -> str:
        return f"UserContext(user_id={self.user_id!r}, base_dir={self.base_dir})"


__all__ = [
    "UserContext",
    "UserMetadata",
    "ConversationContext",
]
