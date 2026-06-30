"""core.md 解析器"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from nanobee.utils.logger import logger



_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
"""模板文件目录"""


class CoreMDParser:
    """解析 core.md 文件

    core.md 结构：
    - ## Soul（人格）
    - ## Rules（行为规则）

    默认模板位于 ``templates/core_default.md``，不硬编码在代码中。
    """

    # 支持的段落名
    KNOWN_SECTIONS = ["Soul", "Rules"]

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

        总是重新读取文件，确保哈希值反映磁盘上的最新内容。

        Returns:
            十六进制哈希字符串
        """
        with open(self.core_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @classmethod
    def create_default(cls, output_path: str | Path) -> Path:
        """创建默认的 core.md 模板（从外部模板文件读取）。

        Args:
            output_path: 输出路径

        Returns:
            创建的文件路径
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        template_path = _TEMPLATE_DIR / "core_default.md"
        if template_path.is_file():
            content = template_path.read_text(encoding="utf-8")
            logger.debug("从模板文件读取默认 core.md: {}", template_path)
        else:
            content = cls._builtin_fallback()
            logger.warning("模板文件不存在，使用内置回退: {}", template_path)

        output_path.write_text(content, encoding="utf-8")
        logger.info("已创建默认 core.md: {}", output_path)
        return output_path

    @classmethod
    def _builtin_fallback(cls) -> str:
        """内置回退模板（模板文件丢失时的保底内容）。"""
        parts = [
            "# core.md — Nanobee 数字员工的唯一管控文件\n",
            "\n## Soul（人格）\n",
            "\n你是 Nanobee，一个简洁、高效的数字员工。",
            "你的回答应当准确、有用且简洁。\n",
            "\n## Rules（行为规则）\n",
            "\n- 在调用工具前，先思考是否真的需要调用",
            "\n- 如果用户只打招呼，不需要调用任何工具",
            "\n- 保持回答简洁，避免冗余",
            "\n- 任何创建、修改、删除或查询数据的操作",
            "（包括 cron 任务、文件、技能等）都必须调用对应工具",
            "\n- 工具返回的结果是唯一可靠的事实来源",
        ]
        return "".join(parts)
