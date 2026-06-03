"""上下文管道 - 构建 Agent 的系统提示词"""

from __future__ import annotations

import logging
from typing import Any

from nanobee.kernel.core_parser import CoreMDParser

logger = logging.getLogger(__name__)

# 插件类型 → 提示词段标题映射
_PLUGIN_TYPE_STAGE_MAP: dict[str, str] = {
    "memory": "## 记忆",
    "skill": "## 技能",
    "knowledge": "## 知识库",
}


def _map_plugin_stage(plugin: Any) -> str:
    """将插件映射到提示词段标题。

    优先级：插件显式声明的 stage > plugin_type > 兜底。
    """
    stage = getattr(plugin, "stage", None)
    if stage:
        return f"## {stage}"
    plugin_type = getattr(plugin, "plugin_type", "unknown") or \
                  getattr(getattr(plugin, "metadata", None), "plugin_type", "unknown")
    return _PLUGIN_TYPE_STAGE_MAP.get(plugin_type, f"## {plugin_type}")


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

    async def build_with_plugins(
        self,
        context: dict[str, Any],
        user_context: Any,
        plugins: list[Any],
    ) -> str:
        """使用插件 Hook 构建系统提示词（Phase 2 增强版）。

        组装顺序：
        1. [P0] Soul 段：框架内置（core.md Soul 节）— 由现有 Stage 产生
        2. [P10-P30] 业务段：遍历插件调用 contribute_to_prompt，按 plugin_type 分组
           - ## 记忆：memory 类型插件
           - ## 技能：skill 类型插件
           - ## 知识库：knowledge 类型插件
        3. [P40] Rules 段：框架内置（core.md Rules 节）— 由现有 Stage 产生

        每个段有内容时才注入，无内容时跳过。
        向后兼容：不提供 plugins 或 plugins 为空时，行为同 build()。

        Args:
            context: 上下文字典（同 build()）
            user_context: 当前用户上下文（UserContext 实例）
            plugins: 已启用的插件列表

        Returns:
            构建完成的系统提示词
        """
        # 1. 先执行框架内置 Stage（Soul, Rules, Memory）
        system_prompt = await self.build(context)

        if not plugins:
            return system_prompt

        # 2. 收集插件贡献，按段标题分组
        stages_content: dict[str, list[str]] = {}
        for plugin in plugins:
            try:
                content = plugin.contribute_to_prompt(user_context)
            except Exception:
                logger.exception("插件 %s.contribute_to_prompt 出错", getattr(plugin, "name", "?"))
                continue
            if content:
                stage = _map_plugin_stage(plugin)
                stages_content.setdefault(stage, []).append(content)

        if not stages_content:
            return system_prompt

        # 3. 按固定顺序组装插件段
        plugin_sections: list[str] = []
        for stage in ["## 记忆", "## 技能", "## 知识库"]:
            contents = stages_content.get(stage)
            if contents:
                plugin_sections.append(stage + "\n" + "\n\n".join(contents))

        if plugin_sections:
            system_prompt += "\n\n" + "\n\n".join(plugin_sections)

        return system_prompt
