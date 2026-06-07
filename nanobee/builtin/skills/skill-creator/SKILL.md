---
name: skill-creator
description: "创建、编辑、管理技能（Skill）。技能是 Markdown 文档资产，定义 AI 的行为模式和工作流。"
---

# Skill Creator

你可以通过读写文件来管理技能。技能存储在 `skills/<技能名称>/SKILL.md` 目录下。

## SKILL.md 格式

每个技能是一个 Markdown 文件，包含 YAML frontmatter 元数据和正文。

```markdown
---
name: my-skill
description: "简短描述技能的用途"
author: "可选，创建者"
compatibility: "可选，兼容性说明"
license: "可选，许可证"
---

# 技能正文

这里写 Markdown 格式的指令。
```

## 创建技能

使用 `write_file` 工具：

```
write_file(path="skills/my-skill/SKILL.md", content="---\nname: my-skill\ndescription: \"我的技能\"\n---\n\n# 正文")
```

## 更新技能

直接覆盖文件：`write_file(path="skills/my-skill/SKILL.md", content="...")`

## 删除技能

删除整个目录：`skills/<技能名称>/`

## 校验

创建后可用 `list_skills` 工具确认技能已被框架发现。

## 命名规范

- kebab-case：仅小写字母、数字、连字符
- 不以连字符起止
- 示例：`git-log-analyzer`、`weekly-report-generator`
