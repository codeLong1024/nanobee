"""
skill_static 参考插件 —— 读取 skills.md 原样注入技能段

Phase 3 参考插件：不实现 SkillPlugin 抽象接口（保持极简），
仅通过 Hook 机制从文件读取内容注入 System Prompt。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobee.plugins.base import NanobeePlugin

logger = logging.getLogger(__name__)


class SkillStaticPlugin(NanobeePlugin):
    """极简技能插件：将 skills.md 内容原样注入 System Prompt 的技能段。

    结构与 ``memory_echo`` 一致，仅文件名为 ``skills.md``，
    通过 ``stage = "技能"`` 控制注入到 ``## 技能`` 段。
    """

    name = "skill_static"
    version = "1.0.0"
    plugin_type = "echo"
    stage = "技能"

    def contribute_to_prompt(self, context: Any) -> str | None:
        """读取 {context.base_dir}/skills.md，原样返回。

        Args:
            context: UserContext 实例，需包含 base_dir 属性

        Returns:
            文件内容（strip 后），文件不存在或读取失败时返回 None
        """
        try:
            base_dir = getattr(context, "base_dir", None)
            if base_dir is None:
                return None
            skills_file = Path(base_dir) / "skills.md"
            if not skills_file.exists():
                return None
            content = skills_file.read_text(encoding="utf-8").strip()
            return content if content else None
        except Exception:
            logger.exception("skill_static 读取 skills.md 失败")
            return None
