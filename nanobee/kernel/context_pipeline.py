"""上下文管道 - 构建 Agent 的系统提示词"""

from __future__ import annotations

import logging
from typing import Any

from nanobee.kernel.core_parser import CoreMDParser

logger = logging.getLogger(__name__)


class PipelineStage:
    """管道阶段基类"""

    def __init__(self, priority: int = 100):
        """初始化

        Args:
            priority: 优先级（数字越小越先执行）
        """
        self.priority = priority

    async def process(self, context: dict[str, Any]) -> dict[str, Any]:
        """处理上下文

        Args:
            context: 上下文字典，包含 messages, system_prompt 等

        Returns:
            处理后的上下文
        """
        return context


class SoulStage(PipelineStage):
    """注入 Soul 段（人格定义）"""

    def __init__(self, core_md_path: str):
        super().__init__(priority=10)  # 最高优先级，最先注入
        self.core_md_path = core_md_path

    async def process(self, context: dict[str, Any]) -> dict[str, Any]:
        parser = CoreMDParser(self.core_md_path)
        soul_content = parser.soul
        if soul_content:
            if "system_prompt" not in context:
                context["system_prompt"] = ""
            context["system_prompt"] = soul_content + "\n\n" + context["system_prompt"]
        return context


class RulesStage(PipelineStage):
    """注入 Rules 段（行为规则）"""

    def __init__(self, core_md_path: str):
        super().__init__(priority=20)
        self.core_md_path = core_md_path

    async def process(self, context: dict[str, Any]) -> dict[str, Any]:
        parser = CoreMDParser(self.core_md_path)
        rules_content = parser.rules
        if rules_content:
            if "system_prompt" not in context:
                context["system_prompt"] = ""
            context["system_prompt"] += "\n\n## 行为规则\n\n" + rules_content
        return context


class MemoryStage(PipelineStage):
    """注入记忆内容"""

    def __init__(self, kernel: Any):
        super().__init__(priority=30)
        self.kernel = kernel

    async def process(self, context: dict[str, Any]) -> dict[str, Any]:
        # TODO: MVP 后从 MemoryPlugin 检索记忆
        return context


class ContextPipeline:
    """上下文处理管道

    按优先级顺序执行多个 Stage，构建最终的系统提示词。
    """

    def __init__(self, kernel: Any):
        """初始化

        Args:
            kernel: NanobeeKernel 实例
        """
        self.kernel = kernel
        self._stages: list[PipelineStage] = []

        # 注册默认 Stage
        core_md_path = kernel.config.get("core_md_path", "core.md")
        self.register(SoulStage(core_md_path))
        self.register(RulesStage(core_md_path))
        self.register(MemoryStage(kernel))

    def register(self, stage: PipelineStage) -> None:
        """注册管道阶段

        Args:
            stage: 管道阶段实例
        """
        self._stages.append(stage)
        # 按优先级排序
        self._stages.sort(key=lambda s: s.priority)

    async def build(self, context: dict[str, Any]) -> str:
        """构建系统提示词

        Args:
            context: 上下文字典

        Returns:
            构建完成的系统提示词
        """
        # 初始化上下文
        if "system_prompt" not in context:
            context["system_prompt"] = ""

        # 依次执行所有 Stage
        for stage in self._stages:
            context = await stage.process(context)

        return context.get("system_prompt", "")
