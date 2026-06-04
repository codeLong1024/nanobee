# nanobee 🐝

> 极简 AI Agent 微内核框架 —— 继承自 [nanobot](https://github.com/HKUDS/nanobot)，聚焦核心基础设施的精简重构。

---

## 项目定位

大多数 AI Agent 框架是"大而全"的——自带的记忆策略、梦境调度、人设漂移检测，但你往往用不上，或者想用自己的方案。

**nanobee 反其道而行之**：框架只做三件事——**路由、隔离、拼装**。所有业务逻辑（记忆、技能、知识库、审计、工具过滤）都是插件的事。

当框架只做容器时，**逻辑极简，几乎零业务 Bug**，也不会为了业务便利而开安全后门。

## 核心设计原则

| 原则 | 含义 |
|------|------|
| **内核无知论** | 框架不知道"记忆"是什么、不知道"技能"怎么截断，只负责通过 Hook 拿到字符串，并把它塞进 System Prompt 对应的坑位 |
| **隔离绝对论** | P0 唯一生死线——物理隔离 + 沙箱拦截。哪怕所有插件崩溃，Alice 也绝对无法越权读取 Bob 一字节文件 |
| **Hook 侵入论** | 在消息流转的所有关键切面留下接口，让插件可以劫持上下文、动态修改工具列表、监听生命周期 |

## 当前状态

**版本 v0.1.0** — 微内核基建全部完成，可运行完整对话循环，测试覆盖率持续增长。

```
🔧 Phase 1 多租户隔离内核    ✅ 完成（85 测试）
🔧 Phase 2 Hook 机制          ✅ 完成（17 测试）
🔧 Phase 3 参考插件            ✅ 完成（18 测试）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总计 120 测试全部通过
```

### 已完成功能

- **NanobeeKernel** — 统一入口，管理插件生命周期、消息路由、灵魂文件保护
- **Agent 循环** — 完整状态机驱动（RESTORE → BUILD → RUN → SAVE → RESPOND）
- **多租户隔离内核** — LockManager 并发锁、UserContext 元数据、ContextRouter 路由、ContextSandbox 沙箱、ToolCollector 过滤
- **Plugin Hook 机制** — 5 个核心契约接口，插件可在关键切面注入逻辑
3 个参考插件（memory_echo / skill_static / audit_logger）端到端验证
- **上下文管理** — 按用户物理隔离的目录结构与上下文元数据
- **灵魂守卫** — 三层保护（chmod 444 + 写入拦截 + SHA-256）
- **插件管理器** — 扫描、加载、启用/禁用生命周期
- **LLM Provider** — Anthropic、OpenAI、Azure、Bedrock、GitHub Copilot、兼容接口
- **MCP 桥接** — 连接 MCP 服务器注册工具
- **CLI 命令** — `nanobee run` / `hub` / `plugin`

### 内置插件

| 插件 | 类型 | 状态 | 说明 |
|------|------|------|------|
| `channel_cli` | Channel | ✅ | 命令行交互通道 |
| `tool_echo` | Tool | ✅ | 回显测试工具 |
| `memory_file` | Memory | ✅ | JSONL 文件记忆存储 |
| `memory_echo` | Echo | ✅ | Phase 3 参考：读取 memory.txt 注入记忆段 |
| `skill_static` | Echo | ✅ | Phase 3 参考：读取 skills.md 注入技能段 |
| `audit_logger` | Audit | ✅ | Phase 3 参考：on_message_completed 审计日志 |
| `channel_http` | Channel | 🚧 | HTTP 通道（待完善） |
| `tool_fs` | Tool | 🚧 | 文件系统工具（待实现） |
| `tool_shell` | Tool | 🚧 | Shell 执行工具（待实现） |
| `tool_web` | Tool | 🚧 | Web 工具（待实现） |

## 快速开始

```bash
# 安装
pip install -e .

# 配置
cp nanobee.yaml.example nanobee.yaml
# 编辑 nanobee.yaml，填入 API Key

# 运行对话
nanobee run
# nanobee run -v       # 详细日志
# nanobee run -c /path/to/config.yaml
```

## 架构

```
用户输入
    │
    ▼
ChannelPlugin ──▶ EventBus ──▶ NanobeeKernel
                                    │
                         ┌──────────┼──────────────┐
                         ▼          ▼              ▼
                   PluginManager  ContextManager  SoulGuard
                         │          ▼
                         ▼     AgentLoop (状态机)
                   ToolRegistry     │
                         │    ┌─────┴─────┐
                         ▼    ▼           ▼
                    AgentRunner  LLM Provider
                         │         │
                         └─────────┴──────────┘
                                    │
                                    ▼
                               用户响应
```

**核心组件速览：**

| 组件 | 职责 |
|------|------|
| `NanobeeKernel` | 统一入口，管理插件生命周期、消息路由 |
| `AgentLoop` | 状态机驱动循环（RESTORE → RUN → SAVE → RESPOND） |
| `AgentRunner` | LLM 调用 + 工具执行迭代（含 Hook 调用） |
| `ContextManager` | 多租户上下文隔离（每个用户独立目录） |
| `ContextPipeline` | System Prompt 构建（Soul → 插件注入 → Rules） |
| `PluginManager` | 插件扫描、加载、生命周期控制 |
| `EventBus` | 异步事件发布/订阅 |
| `SoulGuard` | 灵魂文件三层保护 |
| `LockManager` | 按用户粒度的异步并发锁 |
| `ContextRouter` | 多租户路由（channel:chat_id → user_id） |
| `ContextSandbox` | 沙箱拦截路径逃逸（`../` 检测） |
| `ToolCollector` | 工具白/黑名单双重过滤 |
| `PluginHook` | 5 个 Hook 契约（Prompt/工具列表/前置拦截/后置拦截/消息完成） |

### Pipeline 如何组装 System Prompt

```
[P0]  Soul 段     ← core.md Soul 节（框架内置）
[P10] 记忆段       ← 遍历插件 contribute_to_prompt → stage="记忆"
[P20] 技能段       ← 遍历插件 contribute_to_prompt → stage="技能"
[P30] 知识库段     ← 遍历插件 contribute_to_prompt → stage="知识库"
[P40] Rules 段     ← core.md Rules 节 + 用户隔离铁律（框架内置）
```

每个段有内容才注入，无内容跳过。框架只做**拼装**，不做任何业务理解。

## 插件开发

插件只需继承 `NanobeePlugin`，覆盖需要的 Hook 方法：

```python
from nanobee.plugins.base import NanobeePlugin

class MyPlugin(NanobeePlugin):
    name = "my_plugin"
    version = "1.0.0"
    plugin_type = "echo"
    stage = "记忆"  # 控制注入到 System Prompt 哪个段

    def contribute_to_prompt(self, context) -> str | None:
        """向 ## 记忆 段注入内容"""
        memory_file = context.base_dir / "memory.txt"
        if memory_file.exists():
            return memory_file.read_text(encoding="utf-8")
        return None
```

### 可覆盖的 5 个 Hook 方法

| Hook | 时机 | 用途 |
|------|------|------|
| `contribute_to_prompt(context)` | System Prompt 构建时 | 向指定段注入文本 |
| `contribute_to_tools(context, tools)` | 工具列表构建时 | 动态增删工具 |
| `on_pre_invoke(context, name, args)` | 工具执行前 | 参数修改、鉴权 |
| `on_post_invoke(context, name, result)` | 工具执行后 | 结果修改、副作用 |
| `on_message_completed(context, messages)` | 对话轮次结束 | 审计日志、后台整理 |

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试（120 个）
python -m pytest tests/ -v --tb=short

# 查看覆盖率
python -m pytest tests/ --cov=nanobee --cov-report=term-missing
```

| 测试模块 | 用例数 | 覆盖场景 |
|---------|-------|---------|
| `test_kernel.py` | 3 | 内核初始化与基础操作 |
| `test_plugin_system.py` | 10 | 插件加载/生命周期/管理器 |
| `test_context_security.py` | 4 | 上下文安全边界 |
| `test_e2e.py` | 4 | 端到端集成 |
| `test_lock_manager.py` | 6 | 按用户并发锁隔离 |
| `test_user_context.py` | 11 | 用户上下文/元数据 |
| `test_router.py` | 12 | 路由解析/降级/自定义 |
| `test_sandbox.py` | 16 | 路径逃逸拦截/边界 |
| `test_tool_collector.py` | 11 | 白/黑名单过滤 |
| `test_phase1_acceptance.py` | 9 | 多租户隔离验收 |
| `test_phase2_acceptance.py` | 17 | Hook 机制验收 |
| `test_phase3_acceptance.py` | 18 | 参考插件验收 |

## 目录结构

```
nanobee/
├── agent/               # Agent 核心引擎
│   ├── loop.py          # 状态机驱动的消息循环
│   ├── runner.py        # LLM 调用 + 工具执行
│   └── ...
├── builtin/             # 内置插件（参考实现）
│   ├── audit_logger/    # 审计日志参考插件
│   ├── channel_cli/     # CLI 通道
│   ├── channel_http/    # HTTP 通道 🚧
│   ├── memory_echo/     # 记忆回显参考插件
│   ├── memory_file/     # JSONL 记忆存储
│   ├── skill_static/    # 静态技能参考插件
│   ├── tool_echo/       # 回显测试工具
│   ├── tool_fs/         # 文件系统工具 🚧
│   ├── tool_shell/      # Shell 工具 🚧
│   └── tool_web/        # Web 工具 🚧
├── cli/                 # 命令行入口
├── config/              # 配置加载与 Schema
├── kernel/              # 微内核核心
│   ├── __init__.py      # 统一导出
│   ├── kernel.py        # NanobeeKernel
│   ├── context_manager.py  # 上下文管理器
│   ├── context_pipeline.py # 提示词构建管道
│   ├── lock_manager.py     # 按用户并发锁
│   ├── router.py           # 多租户路由
│   ├── sandbox.py          # 路径逃逸沙箱
│   ├── soul_guard.py       # 灵魂文件守卫
│   ├── tool_collector.py   # 工具过滤
│   └── user_context.py     # 用户上下文
├── plugins/             # 插件接口定义
│   ├── base.py          # NanobeePlugin 基类（含 5 个 Hook 默认实现）
│   ├── hook_mixin.py    # PluginHookMixin（Hook 契约文档）
│   ├── channel.py       # ChannelPlugin 接口
│   ├── memory.py        # MemoryPlugin 接口
│   ├── skill.py         # SkillPlugin 接口
│   ├── tool.py          # ToolPlugin 接口
│   ├── dream.py         # DreamPlugin 接口
│   └── knowledge.py     # KnowledgePlugin 接口
├── providers/           # LLM 提供商实现
├── security/            # 安全策略（占位）
├── templates/           # 模板文件
└── utils/               # 工具函数
```

## 被剥离 —— 留给插件生态

以下内容**不属于框架核心**，留给社区或应用层插件实现：

- 记忆截断策略（由 `memory-vector`、`memory-summarizer` 等插件实现）
- 梦境整理系统（由后台 Daemon 插件实现）
- 人设漂移检测（由 `drift-detector` 插件实现）
- Subagent 委派工具（由 `tool-subagent` 插件实现）
- 复杂的 facts.json / episodic.json 格式化

框架的责任是**画地为牢（隔离）**，插件的自由是**破土动工（注入）**。

## 开发

```bash
# 环境要求
# Python >= 3.11

# 必须使用虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 开发工具
pip install pre-commit mypy pydocstyle pytest-cov
```

详细贡献指南请见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

nanobee 基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT License）衍生开发。
本项目的代码继承、修改和新增内容均遵循 MIT 许可证。

原始项目版权：Copyright (c) 2025-present Xubin Ren and the nanobot contributors
