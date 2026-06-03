"""core.md 解析器"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CoreMDParser:
    """解析 core.md 文件

    core.md 结构：
    - ## Soul（人格）
    - ## Rules（行为规则）
    - ## Memory Policy（记忆策略）[MVP 后实现]
    - ## Context Bindings（上下文绑定）[MVP 后实现]
    """

    # 支持的段落名
    KNOWN_SECTIONS = ["Soul", "Rules", "Memory Policy", "Context Bindings"]

    def __init__(self, core_md_path: str | Path):
        """初始化解析器

        Args:
            core_md_path: core.md 文件路径
        """
        self.core_md_path = Path(core_md_path)
        self._sections: dict[str, str] = {}
        self._raw_content: str = ""

    def parse(self) -> dict[str, str]:
        """解析 core.md 文件

        Returns:
            段落名 → 段落内容的字典
        """
        if not self.core_md_path.exists():
            raise FileNotFoundError(f"core.md 不存在: {self.core_md_path}")

        with open(self.core_md_path, "r", encoding="utf-8") as f:
            self._raw_content = f.read()

        current_section = None
        current_lines: list[str] = []

        for line in self._raw_content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                # 保存上一个段落
                if current_section is not None:
                    self._sections[current_section] = "\n".join(current_lines).strip()

                # 开始新段落
                current_section = stripped[3:].strip()
                current_lines = []
            else:
                current_lines.append(line)

        # 保存最后一个段落
        if current_section is not None:
            self._sections[current_section] = "\n".join(current_lines).strip()

        return self._sections

    @property
    def soul(self) -> str:
        """获取 Soul 段内容"""
        if not self._sections:
            self.parse()
        return self._sections.get("Soul", "")

    @property
    def rules(self) -> str:
        """获取 Rules 段内容"""
        if not self._sections:
            self.parse()
        return self._sections.get("Rules", "")

    def get_section(self, name: str) -> str:
        """获取指定段落内容"""
        if not self._sections:
            self.parse()
        return self._sections.get(name, "")

    def compute_hash(self) -> str:
        """计算 core.md 文件的 SHA-256 哈希

        Returns:
            十六进制哈希字符串
        """
        if not self._raw_content:
            with open(self.core_md_path, "r", encoding="utf-8") as f:
                self._raw_content = f.read()

        return hashlib.sha256(self._raw_content.encode("utf-8")).hexdigest()

    @classmethod
    def create_default(cls, output_path: str | Path) -> Path:
        """创建默认的 core.md 模板

        Args:
            output_path: 输出路径

        Returns:
            创建的文件路径
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        default_content = """# core.md — Nanobee 数字员工的唯一管控文件

## Soul（人格）

你是 Nanobee，一个简洁、高效的数字员工。
你的回答应当准确、有用且简洁。

## Rules（行为规则）

- 在调用工具前，先思考是否真的需要调用
- 如果用户只打招呼，不需要调用任何工具
- 保持回答简洁，避免冗余

## Memory Policy（记忆策略）

TODO: MVP 后实现

## Context Bindings（上下文绑定）

TODO: MVP 后实现
"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(default_content)

        logger.info("已创建默认 core.md: %s", output_path)
        return output_path
