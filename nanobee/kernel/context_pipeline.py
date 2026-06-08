"""上下文管道 - 构建 Agent 的系统提示词

从 kernel.skill_manager 统一读取技能数据，避免路径分裂。
使用 dataclass（PromptBuildContext）替代裸 dict 传递上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobee.kernel.core_parser import CoreMDParser

if TYPE_CHECKING:
    from nanobee.kernel.skill_manager import SkillsLoader

from nanobee.utils.logger import logger


def _map_plugin_stage(plugin: Any) -> str:
    """将插件映射到提示词段标题。

    优先级：插件显式声明的 stage > plugin_type > 兜底。
    框架不做语义理解，plugin_type 直接用作段标题。
    """
    stage = getattr(plugin, "stage", None)
    if stage:
        return f"## {stage}"
    plugin_type = getattr(plugin, "plugin_type", None)
    if plugin_type is None:
        meta = getattr(plugin, "metadata", None)
        plugin_type = getattr(meta, "plugin_type", "unknown") if meta is not None else "unknown"
    return f"## {plugin_type}"


@dataclass
class PromptBuildContext:
    """类型安全的提示词构建上下文，替代裸 dict 传递。

    包含构建 system prompt 所需的全部字段。
    """

    system_prompt: str = ""
    user_context: Any = None
    context_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    def get_system_prompt(self) -> str:
        """获取当前 system prompt（兼容旧 dict 访问）。"""
        return self.system_prompt

    def set_system_prompt(self, value: str) -> None:
        """设置 system prompt。"""
        self.system_prompt = value

    def append_system_prompt(self, text: str) -> None:
        """追加内容到 system prompt 末尾。"""
        if self.system_prompt:
            self.system_prompt += "\n\n" + text
        else:
            self.system_prompt = text

    def prepend_system_prompt(self, text: str) -> None:
        """在 system prompt 前面插入内容。"""
        if self.system_prompt:
            self.system_prompt = text + "\n\n" + self.system_prompt
        else:
            self.system_prompt = text

    @staticmethod
    def _from_compat(context: PromptBuildContext | dict[str, Any]) -> PromptBuildContext:
        """将 dict 或 PromptBuildContext 统一为 PromptBuildContext。"""
        if isinstance(context, PromptBuildContext):
            return context
        return PromptBuildContext(
            system_prompt=context.get("system_prompt", ""),
            user_context=context.get("user_context"),
            context_id=context.get("context_id", ""),
            messages=context.get("messages", []),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 dict（向后兼容）。"""
        return {
            "system_prompt": self.system_prompt,
            "user_context": self.user_context,
            "context_id": self.context_id,
            "messages": self.messages,
        }


class PipelineStage:
    """管道阶段基类"""

    def __init__(self, priority: int = 100):
        """初始化

        Args:
            priority: 优先级（数字越小越先执行）
        """
        self.priority = priority

    async def process(
        self,
        context: PromptBuildContext | dict[str, Any],
    ) -> PromptBuildContext | dict[str, Any]:
        """处理上下文

        接受 PromptBuildContext 或兼容 dict（自动转换）。

        Args:
            context: PromptBuildContext 实例或兼容 dict

        Returns:
            处理后的上下文（与输入类型相同：PromptBuildContext → PromptBuildContext, dict → dict）
        """
        return context


class SoulStage(PipelineStage):
    """注入 Soul 段（人格定义）"""

    def __init__(self, core_md_path: str):
        super().__init__(priority=10)  # 最高优先级，最先注入
        self.core_md_path = core_md_path

    async def process(
        self,
        context: PromptBuildContext | dict[str, Any],
    ) -> PromptBuildContext | dict[str, Any]:
        from_dict = isinstance(context, dict)
        ctx = PromptBuildContext._from_compat(context)  # type: ignore[arg-type]
        parser = CoreMDParser(self.core_md_path)
        soul_content = parser.soul
        if soul_content:
            ctx.prepend_system_prompt(soul_content)
        return ctx.to_dict() if from_dict else ctx


class RulesStage(PipelineStage):
    """注入 Rules 段（行为规则 + 用户身份）"""

    def __init__(self, core_md_path: str):
        super().__init__(priority=20)
        self.core_md_path = core_md_path

    async def process(
        self,
        context: PromptBuildContext | dict[str, Any],
    ) -> PromptBuildContext | dict[str, Any]:
        from_dict = isinstance(context, dict)
        ctx = PromptBuildContext._from_compat(context)  # type: ignore[arg-type]
        parser = CoreMDParser(self.core_md_path)
        rules_content = parser.rules

        # 注入用户身份 + 工作目录信息 —— 让 LLM 知道自己的 user_id 和文件操作的基准路径
        user_ctx = ctx.user_context
        if user_ctx is not None:
            user_id = getattr(user_ctx, "user_id", None)
            context_root = getattr(user_ctx, "context_root", None)
            extra_lines: list[str] = []
            if user_id:
                extra_lines.append(f"你的用户 ID 是：`{user_id}`。")
            if context_root:
                extra_lines.append(
                    f"你的工作目录是：`{context_root}`。\n"
                    f"所有相对路径（如 `memory/facts.md`）都基于此目录解析。"
                )
            if extra_lines:
                workspace_section = "\n## 用户身份\n\n" + "\n".join(extra_lines)
                if rules_content:
                    rules_content += workspace_section
                else:
                    rules_content = workspace_section

        if rules_content:
            ctx.append_system_prompt("## 行为规则\n\n" + rules_content)
        return ctx.to_dict() if from_dict else ctx


class SkillStage(PipelineStage):
    """注入 ## 技能 段 —— 从 UserContext 的 skills/ 目录加载技能。

    Skill 是用户知识资产（SKILL.md），非代码插件。
    此 Stage 内置在框架中，不依赖 Plugin 生命周期。
    使用 kernel.skill_manager 统一实例，避免路径分裂。

    Phase 2 增强：加 [SKILL BEGIN/END] 边界标记，
    共享技能 body 每行使用 > 引用包裹。
    """

    def __init__(self, source: SkillsLoader) -> None:
        super().__init__(priority=28)
        self._loader = source

    async def process(
        self,
        context: PromptBuildContext | dict[str, Any],
    ) -> PromptBuildContext | dict[str, Any]:
        """注入技能段 —— 元数据驱动的渐进式/全量注入策略。

        框架不关心技能名称，完全由 SKILL.md frontmatter 中的
        ``full_inject: true`` 声明决定注入策略：
          - full_inject=true  → 元数据 + 完整 body（LLM 需要每次访问）
          - full_inject=false → 仅元数据（LLM 按需读取完整内容）

        同名技能自动去重（用户版优先于内置版），框架不做策略决策。

        Args:
            context: PromptBuildContext 实例或兼容 dict

        Returns:
            处理后的上下文
        """
        from_dict = isinstance(context, dict)
        ctx = PromptBuildContext._from_compat(context)  # type: ignore[arg-type]

        all_skills = self._loader.list_all_skills()
        if not all_skills:
            return ctx.to_dict() if from_dict else ctx

        sections: list[str] = []
        seen_names: set[str] = set()

        for skill in all_skills:
            if skill.meta.name in seen_names:
                continue  # 同名去重：用户版优先（list_all_skills 先内置后用户）
            seen_names.add(skill.meta.name)

            source_tag = "[builtin]" if skill.source == "builtin" else "[user]"

            if skill.meta.full_inject:
                # 全量注入：元数据 + 完整 body（由 frontmatter 声明触发）
                sections.append(
                    f"---\n"
                    f"[SKILL: {skill.meta.name} {source_tag}]\n\n"
                    f"### {skill.meta.name} {source_tag}\n"
                    f"**描述**: {skill.meta.description}\n\n"
                    f"{skill.body}\n"
                )
            else:
                # 渐进式注入：只注入元数据，LLM 按需读取
                source_prefix = f" (@{skill.meta.author})" if skill.meta.author else ""
                lines = [
                    "---",
                    f"[SKILL: {skill.meta.name} {source_tag}]",
                    "",
                    f"### {skill.meta.name}{source_prefix} {source_tag}",
                    "",
                    f"**描述**: {skill.meta.description}",
                    f"**文件**: `skills/{skill.meta.name}/SKILL.md`",
                    "",
                ]
                sections.append("\n".join(lines))

        if not sections:
            return ctx.to_dict() if from_dict else ctx

        skills_section = "\n\n".join(sections)
        ctx.append_system_prompt("## 技能\n\n" + skills_section)
        return ctx.to_dict() if from_dict else ctx


class ContextPipeline:
    """上下文处理管道

    按优先级顺序执行多个 Stage，构建最终的系统提示词。
    """

    def __init__(
        self,
        core_md_path: str,
        skill_loader: SkillsLoader,
        soul_guard: Any | None = None,
    ):
        """初始化

        Args:
            core_md_path: core.md 文件路径
            skill_loader: SkillsLoader 实例
            soul_guard: SoulGuard 实例（可选，用于 build_with_plugins 的安全规则注入）
        """
        self._soul_guard = soul_guard
        self._stages: list[PipelineStage] = []

        # 注册默认 Stage
        self.register(SoulStage(core_md_path))
        self.register(RulesStage(core_md_path))
        self.register(SkillStage(skill_loader))

    def register(self, stage: PipelineStage) -> None:
        """注册管道阶段

        Args:
            stage: 管道阶段实例
        """
        self._stages.append(stage)
        # 按优先级排序
        self._stages.sort(key=lambda s: s.priority)

    async def build(self, context: dict[str, Any] | PromptBuildContext) -> str:
        """构建系统提示词

        Args:
            context: PromptBuildContext 实例或兼容 dict

        Returns:
            构建完成的系统提示词
        """
        ctx = self._to_context(context)
        # 依次执行所有 Stage
        for stage in self._stages:
            ctx = await stage.process(ctx)
        return ctx.system_prompt

    async def build_with_plugins(
        self,
        context: dict[str, Any] | PromptBuildContext,
        user_context: Any,
        plugins: list[Any],
    ) -> str:
        """使用插件 Hook 构建系统提示词（Phase 2 增强版）。

        组装顺序：
        1. [P0] Soul 段：框架内置（core.md Soul 节）
        2. [P10-P30] 内置 Stage：Rules → Skill
        3. 插件段：由插件 stage/plugin_type 决定段标题，按传入顺序排列
        4. [P90] FinalGuard：不可绕过的优先级规则

        每个段有内容时才注入，无内容时跳过。

        Args:
            context: PromptBuildContext 实例或兼容 dict
            user_context: 当前用户上下文（UserContext 实例）
            plugins: 已启用的插件列表

        Returns:
            构建完成的系统提示词
        """
        ctx = self._to_context(context)
        ctx.user_context = user_context

        # 1. 执行所有内置 Stage（Soul, Rules, Skill）
        for stage in self._stages:
            ctx = await stage.process(ctx)

        system_prompt = ctx.system_prompt

        # 2. 收集插件贡献，按段标题分组
        stages_content: dict[str, list[str]] = {}
        for plugin in plugins or []:
            try:
                content = plugin.contribute_to_prompt(user_context)
            except Exception:
                logger.exception("插件 {}.contribute_to_prompt 出错", getattr(plugin, "name", "?"))
                continue
            if content:
                stage = _map_plugin_stage(plugin)
                stages_content.setdefault(stage, []).append(content)

        # 3. 按插件声明顺序组装（不做固定排序）
        if stages_content:
            plugin_sections: list[str] = []
            for stage, contents in stages_content.items():
                plugin_sections.append(stage + "\n" + "\n\n".join(contents))
            if plugin_sections:
                system_prompt += "\n\n" + "\n\n".join(plugin_sections)

        # 4. [P90] 安全规则：从 SoulGuard 读取不可绕过的优先级规则
        if self._soul_guard is not None:
            guard_text = getattr(self._soul_guard, "guard_text", None)
            if guard_text:
                system_prompt += "\n\n" + guard_text

        return system_prompt

    @staticmethod
    def _to_context(
        context: dict[str, Any] | PromptBuildContext,
    ) -> PromptBuildContext:
        """将 dict 或 PromptBuildContext 统一为 PromptBuildContext。"""
        return PromptBuildContext._from_compat(context)

