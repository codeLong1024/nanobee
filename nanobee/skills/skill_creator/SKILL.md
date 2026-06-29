---
name: skill_creator
description: "创建、编辑、打包、校验 Skill 的完整指南。触发: 创建技能/新建skill/写skill/设计技能/编辑技能/修改技能/打包技能/校验技能/验证技能/init_skill/package_skill/quick_validate/技能目录结构/SKILL.md格式/frontmatter/skill creator/命名规范/snake_case/技能初始化"

---

# Skill Creator

通过读写文件管理技能。技能存放在 `skills/<名称>/` 目录下，格式为：

```
<技能名称>/
├── SKILL.md（必需）
│   ├── frontmatter（name + description 必需）
│   └── Markdown 正文
├── scripts/      — 可执行代码，无需读入上下文即可运行
├── references/   — 参考文档，按需加载到上下文
└── assets/       — 输出中使用的文件，不读入上下文
```

## 核心原则

1. **精简至上**：只添加 Agent 尚未掌握的信息。审视每条信息："Agent 真的需要这个解释吗？"宁用简洁示例，不用冗长解释。
2. **自由度匹配**：高自由度用文本说明，中自由度用伪代码/参数化脚本，低自由度用具体脚本。
3. **渐进式披露**：元数据始终在上下文，SKILL.md 正文触发时加载，捆绑资源按需读取。保持 SKILL.md 在 500 行以内，超出则拆分到独立文件。

### Frontmatter 字段

与框架 `SkillMeta` 一致，只支持以下字段：

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | 是 | snake_case，动词导向 |
| `description` | 是 | 说明做什么和何时触发（"何时使用"全放这里，不放正文） |
| `author` | 否 | 创建者 |
| `compatibility` | 否 | 兼容性说明 |
| `full_inject` | 否 | `true` 时每次请求全量注入正文 |

### 资源说明

- **scripts/**：确定性重复操作的可执行代码（Python/Bash），节省 token，无需读入上下文
- **references/**：按需加载的文档（API 参考、数据库模式等），保持 SKILL.md 精简
- **assets/**：最终输出中使用的文件（模板、图片等），不读入上下文

### 禁止文件

只包含直接支持功能的文件。不要创建 README.md、INSTALLATION_GUIDE.md、CHANGELOG.md 等。

## 创建流程

### 1. 理解需求

用具体示例理解技能将如何使用。例如 `pdf_editor` 技能需要明确：支持哪些功能、用户会提什么需求、什么关键词触发。结论清晰即可进入下一步。

### 2. 规划资源

将需求示例转化为可复用资源。示例：
- `pdf_editor` -> 每次旋转 PDF 都要写同样代码 -> `scripts/rotate_pdf.py`
- `frontend_builder` -> 每次写 HTML/React 都需要同样模板 -> `assets/hello_world/`
- `big_query` -> 每次查询都需要重新发现表结构 -> `references/schema.md`

### 3. 初始化

```bash
python3 <本SKILL.md所在目录>/scripts/init_skill.py <名称> --path <路径> [--resources scripts,references,assets] [--examples]
```

脚本会创建目录、生成带 TODO 占位符的 SKILL.md、根据 `--resources` 创建资源目录。

### 4. 编辑

记住技能是为另一个 Agent 创建的，包含对另一 Agent 有益且非显而易见的信息。

参考设计模式：
- 多步骤流程 -> `references/workflows.md`
- 输出格式/质量标准 -> `references/output_patterns.md`

新增脚本必须通过实际运行测试。更新 SKILL.md 的 frontmatter 和正文。

### 5. 打包

```bash
python3 <本SKILL.md所在目录>/scripts/package_skill.py <技能目录> [输出目录]
```

自动校验 frontmatter 格式和命名规范，通过后创建 `.skill` 文件（Zip 格式）。

## 命名规范

- **snake_case**：仅小写字母、数字、下划线
- 不能以数字或下划线开头，不能含连续下划线或末尾下划线
- 长度不超过 64 字符
- 目录名必须与 `name` 字段一致
- 正确：`git_log_analyzer`、`weekly_report`、`pdf_editor`
- 错误：`GitLogAnalyzer`、`my-skill`、`_private_skill`

## 校验

```bash
python3 <本SKILL.md所在目录>/scripts/quick_validate.py <技能目录>
```

也可在打包时自动校验。编辑用户技能后，框架自动发现（缓存 TTL 2 秒），或通过 `invalidate_cache` 立即生效。