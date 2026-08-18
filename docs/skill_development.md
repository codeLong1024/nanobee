# 技能开发指导

Skill（技能）是 Nanobee 的**用户知识资产**——以 Markdown 文档形式编写，框架只做两件事：**发现技能**（扫描 `skills/` 目录）和**按标记注入元数据**。所有策略决策（什么技能值得用、什么时候读 body、怎么组合）由 LLM 自主完成。

这遵循**框架无知论**：框架不关心技能名称、不判断技能用途、不做注入策略决策。框架只读 `full_inject` 标记执行注入策略——这是一个声明式标记，不是框架的"智能"。

---

## 目录

- [技能 vs 插件](#技能-vs-插件)
- [SKILL.md 格式](#skillmd-格式)
- [注入策略（full_inject 声明）](#注入策略full_inject-声明)
- [内置技能（_memory + skill_creator）](#内置技能_memory--skill_creator)
- [编写技能文档](#编写技能文档)
  - [元数据编写规范](#元数据编写规范)
  - [正文编写规范](#正文编写规范)
- [技能发现机制](#技能发现机制)
- [命名规范](#命名规范)
- [查看已安装技能](#查看已安装技能)
- [完整示例](#完整示例)
- [最佳实践](#最佳实践)

---

## 技能 vs 插件

| 维度 | 技能（Skill） | 插件（Plugin） |
|------|--------------|---------------|
| 本质 | Markdown 文档（只读知识） | Python 代码（可执行逻辑） |
| 编写门槛 | 低——会写 Markdown 即可 | 高——需 Python 编程 |
| 注入方式 | 双源扫描 + YAML frontmatter 标记 | PluginManager 加载 + 生命周期 Hook |
| 注入策略 | `full_inject` 标记驱动渐进/全量 | 插件控制（contribute_to_prompt） |
| 更新方式 | 修改 SKILL.md 文件后自动缓存失效 | 需要重新加载插件模块 |
| 来源 | 内置（nanobee/skills/）+ 用户（skills/） | 内置（builtin/）+ 用户（plugins/） |
| 安全性 | 渐进式注入——body 不入 system prompt | 全量注入——代码执行在沙箱内 |

---

## SKILL.md 格式

每个技能是一个 `SKILL.md` 文件，包含 YAML frontmatter 元数据和 Markdown 正文：

```markdown
---
name: my-skill
description: "简短描述技能的用途"
author: "@creator"
full_inject: false
---

# 技能正文

这里写 Markdown 格式的指令。

## 使用场景

描述什么时候应该使用这个技能。
```

### frontmatter 字段

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `name` | 是 | string | 技能名称，仅小写字母、数字、连字符（kebab-case） |
| `description` | 是 | string | 简短描述用途，最多 1024 字符。LLM 通过此字段判断是否需要读取技能 body |
| `author` | 否 | string | 创建者名称，如 `@username` |
| `full_inject` | 否 | bool | `true` 时 body 全量注入 system prompt；`false`（默认）仅注入元数据 |
| `compatibility` | 否 | string | 兼容性说明，如 `"requires Python >= 3.10"` |
| `license` | 否 | string | 许可证，如 `"MIT"` |

### 特殊的 `_memory` 技能

以 `_memory` 命名的技能会显示在技能列表最前（按字母排序），无其他特殊待遇。

框架不识别任何硬编码的技能名称。`full_inject` 是唯一的注入策略声明标记。

---

## 注入策略（full_inject 声明）

框架遵循**框架无知论**——不关心技能名称、不判断"这个技能是否应该全量注入"。策略通过 frontmatter 的 `full_inject` 标记声明，框架只读标记、不懂含义。

```
full_inject: true  ──▶  元数据 + body 全量注入 system prompt
                        适用于每轮对话都必须看到的指令
                        例如：记忆管理策略、安全规则

full_inject: false ──▶  仅注入元数据（name + description）
                        正文由 LLM 自主按需读取
                        适用于大部分普通技能
                        从根源杜绝注入攻击（恶意 body 不进 system prompt）
```

### 全量注入（full_inject: true）

适用于 LLM 每次对话都必须看到 body 内容的技能：

```markdown
---
name: _memory
description: "长期记忆管理策略"
full_inject: true
---
```

框架将完整 body 注入 system prompt 的 `## Skills` 段。LLM 在每轮对话中都能看到完整指令。

### 渐进式注入（full_inject: false，默认）

```markdown
---
name: git-log-analyzer
description: "分析 git 提交历史并生成周报"
full_inject: false
---
```

框架只注入到 system prompt：

```markdown
## Skills

### git-log-analyzer
- **name**: git-log-analyzer
- **description**: 分析 git 提交历史并生成周报
- **source**: [user]
- **author**: @user
- **path**: skills/git-log-analyzer/SKILL.md
```

LLM 看到描述后，自主决定是否通过文件工具读取正文。这有两个好处：

1. **节省 token**：system prompt 体积减少 60-80%
2. **安全**：恶意 body 永远无法进入 system prompt（注入攻击免疫）

### 选择策略

| 场景 | 推荐策略 |
|------|---------|
| 记忆管理策略（`_memory`） | `full_inject: true` |
| 安全/行为规则 | `full_inject: true` |
| 通用知识（编程语言指南） | `full_inject: false` |
| 模板/代码片段 | `full_inject: false` |
| 分析/报告生成流程 | `full_inject: false` |
| 用户偶尔使用的技能 | `full_inject: false` |

---

## 内置技能（_memory + skill_creator）

框架打包两个内置技能，位于 `nanobee/skills/`（只读，不可覆盖）：

### `_memory` — 兜底记忆策略

- **作用**：LLM 自主管理 memory 文件（读取 `memory/facts.md` 和 `memory/scratchpad.md`）
- **注入方式**：`full_inject: true` —— 全量注入 system prompt
- **说明**：不依赖任何插件，纯 LLM 驱动。当用户添加同名技能到 `skills/` 时，双方都会显示（用户版 autocomplete）

### `skill_creator` — 技能创建教程

- **作用**：教 LLM 如何编写、创建、管理技能
- **注入方式**：渐进式注入（仅元数据），LLM 看到描述后按需读取 body
- **说明**：如果 LLM 已经知道如何创建技能，可以不读取此文档

---

## 编写技能文档

### 元数据编写规范

```yaml
---
name: weekly-report-generator
description: "从 git 提交记录和项目文件自动生成周报 Markdown，包含本周进展、问题和下步计划"
author: "@team-lead"
full_inject: false
---
```

**规范**：

- `name` 必须 kebab-case
- `description` 应当让 LLM 一眼判断是否需要用此技能：包含触发场景、做什么、输出什么
- `description` 禁止包含 `<` `>` 字符
- `full_inject` 仅用于"LLM 每轮对话必须看到"的场景

### 正文编写规范

技能正文直接写 Markdown，建议结构：

```markdown
# 技能名称

一句话说明这个技能的作用。

## 触发条件

描述在什么情况下 LLM 应该使用这个技能。

## 使用步骤

1. 第一步做什么
2. 第二步做什么
3. ...

## 输出格式

描述输出应该是什么样的格式。

## 注意事项

- 注意点 1
- 注意点 2
```

**技巧**：

- **指令清晰具体**：LLM 按指令执行，模糊的指令导致不可预测结果
- **给出例子**：示例胜过千言万语，LLM 理解和模仿能力强
- **明确触发条件**：LLM 需要知道"什么时候用这个技能"
- **控制长度**：渐进式注入时，LLM 通过 `description` 判断是否值得读取。冗长的正文会降低 LLM 读取意愿

---

## 技能发现机制

SkillsLoader 从两个来源发现技能（2 秒 TTL 文件系统缓存）：

```
来源 1: nanobee/skills/   ← 框架内置，只读
    nanobee/skills/
      ├── _memory/SKILL.md
      └── skill_creator/SKILL.md

来源 2: user skills/      ← 用户添加，可写
    <user_context>/skills/
      ├── my-skill/SKILL.md
      └── weekly-report/SKILL.md

    注意：用户技能存放在每个用户的上下文目录下（users/<user_id>/skills/），
    非全局 <work_dir>/skills/（旧路径已废弃）。
```

### 同名冲突处理

```text
skills/git-helper/SKILL.md          → [user]
nanobee/skills/git-helper/  → 不创建（同名时双方都显示，标注来源）
```

### 缓存机制

- `SkillsLoader` 维护 2 秒 TTL 的文件系统缓存
- 用户修改技能文件后，缓存会基于 mtime 自动失效，也可通过 `invalidate_cache()` 手动刷新
- `_memory` 技能的缓存修改通过内置的 CacheConflictCheck hook 自动刷新

---

## 命名规范

- **kebab-case**：仅小写字母、数字、连字符，如 `weekly-report-generator`
- **不以连字符起止**
- **不要使用 `_memory` 等特殊前缀**（框架不对名称做特殊处理，`_memory` 只是按字母排序靠前）
- **建议**：使用描述性名称，如 `git-log-analyzer`、`pr-reviewer`、`docker-compose-helper`

---

## 查看已安装技能

框架在构建 system prompt 前，会将所有技能元数据（name + description + 来源 + 文件路径）注入到 LLM 上下文的 `## 技能` 段，格式如下：

```markdown
## 技能

<skill name="git-log-analyzer" source="user" author="@team-lead" file=".../skills/git-log-analyzer/SKILL.md">
**描述**: 分析 git 提交历史并生成周报
**文件**: `.../skills/git-log-analyzer/SKILL.md`
</skill>

<skill name="_memory" source="builtin" full_inject="true" file=".../skills/_memory/SKILL.md">
**描述**: 长期记忆管理策略
**文件**: `.../skills/_memory/SKILL.md`
</skill>
```

LLM 看到元数据后，自主决定是否通过 `write_file` 读取正文。

---

## 完整示例

### 示例 1：代码审查技能

```markdown
---
name: pr-reviewer
description: "审查 Pull Request 代码变更，输出可操作的反馈，包括代码质量、安全风险和优化建议"
author: "@tech-lead"
full_inject: false
---

# Code Reviewer

审查代码变更并给出结构化反馈。

## 触发条件

当用户要求审查代码、PR 或提交时，或对代码质量提出疑问时。

## 审查步骤

1. **理解变更意图**：读取相关文件和代码上下文
2. **检查以下方面**：
   - 代码正确性：逻辑是否正确
   - 安全性：敏感信息硬编码、注入风险
   - 性能：不必要的循环、冗余查询
   - 可维护性：命名、注释、复杂度
3. **输出格式**：
   ```
   ## 审查摘要
   - 文件变更数: n
   - 严重问题: n
   - 建议: n

   ## 严重问题
   - [严重] 问题描述 + 修复建议

   ## 建议
   - [建议] 问题描述 + 改进方案
   ```

## 注意事项

- 对每个问题给出具体的代码示例
- 区分"必须修"和"建议修"
- 保持建设性语气
```

### 示例 2：安全规则技能（全量注入）

```markdown
---
name: security-guard
description: "通用安全规则：禁止读取敏感文件、禁止执行危险命令、禁止网络请求未经检查"
author: "@admin"
full_inject: true
---

# 安全规则

以下规则在所有操作中必须遵守：

1. **禁止** 读取 /etc/passwd、/etc/shadow 等系统敏感文件
2. **禁止** 执行 rm -rf、dd 等破坏性命令
3. **禁止** 向互联网 POST 用户代码或文件内容
4. 操作外部数据前必须获得用户确认
5. 使用 `write_file` 修改文件前必须先读取确认当前内容

违反上述规则将导致操作被拒绝。
```

---

## 最佳实践

1. **元数据为王**：`description` 是 LLM 判断是否读取你技能的唯一线索。写得清晰直白，包含触发条件
2. **渐进式为主**：除非技能是 LLM 每轮对话都必须看到的安全/记忆规则，否则始终使用 `full_inject: false`
3. **善用示例**：LLM 从示例中学习效果远好于抽象描述
4. **结构化正文**：使用标题（`##`/`###`）、列表、代码块组织内容。LLM 擅长解析结构化 Markdown
5. **保持简洁**：一个技能只做一件事。如果正文很长，考虑拆分为多个技能
6. **明确触发条件**：在每个技能的开头说明"什么情况下使用"，帮助 LLM 在元数据已读的情况下做决策
7. **避免注入攻击**：由于渐进式注入 body 不进 system prompt，技能文档天然免疫注入攻击
8. **可组合性**：让 LLM 能自由组合多个技能使用，而不是设计互相排斥的技能
9. **同名不覆盖**：如果需要在内置技能基础上调整，在 `skills/` 创建同名技能，双方都会展示，LLM 自然选择更合适的版本
10. **测试技能**：创建技能后通过与 LLM 对话验证它是否能正确读取和使用你的技能

### 常见陷阱

| ❌ 避免 | ✅ 推荐 |
|---------|--------|
| `full_inject: true` 滥用，将所有技能全量注入 | 只在需要每轮可见时才设为 `true` |
| `description: "什么都干"` 太模糊 | `description: "生成每周代码审查报告，包含质量统计和团队表现"` |
| 技能正文不包含触发条件，LLM 永远不知道何时使用 | 明确"什么情况下使用这个技能" |
| body 中包含恶意指令 | 渐进式注入天然免疫——仅 inject metadata |
| 使用 `_memory` 之外的特定前缀命名字段 | 框架无特殊含义，直接写业务名称即可 |

---

## 附录：技能注入的完整路径

```
LLM 请求进入
    │
    ▼
ContextPipeline.build()
    │
    ├── SoulStage         ← core.md [Soul]
    ├── RulesStage        ← core.md [Rules]
    ├── SkillStage        ← SkillsLoader 扫描两个来源
    │   │
    │   ├── full_inject?  →  [YES] 注入 name + description + body
    │   │                       [NO]  只注入 name + description（渐进式）
    │   │
    │   └── 输出到 ## Skills 段
    │
    ├── [插件段]           ← 插件 contribute_to_prompt
    │
    └── FinalGuardStage   ← SoulGuard rules
```

**技能目录扫描来源**：

```
/workspace/
├── skills/                     ← 用户技能（可写）
│   ├── my-skill/SKILL.md
│   └── pr-reviewer/SKILL.md
│
└── .venv/.../nanobee/skills/  ← 内置技能（只读）
    ├── _memory/SKILL.md
    └── skill_creator/SKILL.md
```
