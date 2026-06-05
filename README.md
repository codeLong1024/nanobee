# nanobee 🐝

> 极简 AI Agent 微内核框架 —— 继承自 [nanobot](https://github.com/HKUDS/nanobot)，聚焦核心基础设施的精简重构。

---

## 核心理念

大多数 AI Agent 框架是"大而全"的——自带记忆策略、梦境调度、人设漂移检测，但你可能用不上，或者想用自己的方案。

**nanobee 反其道而行之**：框架只做三件事——**路由（Routing）、隔离（Isolation）、拼装（Composition）**。所有业务逻辑（记忆、技能、知识库、审计、工具过滤）都是插件的事。

| 原则 | 含义 |
|------|------|
| **内核无知论** | 框架不知道"记忆"是什么、不知道"技能"怎么截断，只负责通过 Hook 拿到字符串塞进 System Prompt 对应的坑位 |
| **隔离绝对论** | P0 唯一生死线——物理隔离 + 沙箱拦截。哪怕所有插件崩溃，Alice 也绝无法越权读取 Bob 一字节文件 |
| **Hook 侵入论** | 在消息流转的所有关键切面留下接口，让插件劫持上下文、动态修改工具列表、监听生命周期 |

## 项目状态

**版本 v0.1.0** — 核心框架（微内核、Agent 引擎、LLM Provider、插件体系）已通过 **200+ 单元测试**验证。

```
Kernel 内核     ████████████████████████████████  95%
Agent 引擎      ████████████████████████████████ 100%
LLM Providers   ████████████████████████████████ 100%
内置插件        ████████████████████████████████  90%
CLI 命令        ████████████████████████░░░░░░░░  60%
测试覆盖        ████████████████████████████████ 100%
```

### 已完成功能

- **NanobeeKernel** — 统一入口，管理插件生命周期、消息路由、灵魂文件保护
- **多租户隔离内核** — LockManager 并发锁、UserContext 元数据、ContextRouter 路由、ContextSandbox 沙箱、ToolCollector 双重过滤
- **Agent 状态机** — 6 态驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND），支持流式输出、中轮注入、并发锁
- **Plugin Hook 机制** — 5 个核心契约接口，插件可在关键切面注入逻辑
- **技能管理** — Skill 数据模型 + SkillManager，用户通过对话创建/编辑/删除技能，支持 visibility 共享
- **上下文管理** — 按用户物理隔离的目录结构（context.yaml / history.jsonl / work / memory）
- **灵魂守卫** — 三层保护（chmod 444 + 写入拦截 Hook + SHA-256 哈希校验）
- **插件管理器** — 扫描、加载（含依赖拓扑排序）、启用/禁用/卸载生命周期
- **LLM Provider** — Anthropic、OpenAI、Azure、Bedrock、GitHub Copilot、OpenAI 兼容接口、30+ 模型规格注册
- **MCP 桥接** — 连接 MCP 服务器注册工具，支持 stdio / SSE / Streamable HTTP 三种传输协议
- **CLI 命令** — `nanobee run`（交互式对话）、`hub` / `plugin` 子命令（骨架）

### 尚不完整的功能

| 模块 | 说明 | 优先级 |
|------|------|--------|
| `cli/plugin.py` | `create/list/enable/disable` 子命令仅有 echo 占位 | 中 |
| `cli/hub.py` | `search/install/uninstall` 子命令仅有 echo 占位 | 低 |
| `builtin/channel_http` | HTTP 通道仅有空壳方法 | 中 |
| `kernel/dream_scheduler.py` | 梦境调度器存根（21行），未集成 | 低 |
| `kernel/personality.py` | 人格指纹模块存根（20行） | 低 |
| `security/` | 安全策略模块为空 | 中 |

## 快速开始

### Linux / macOS

```bash
# 环境要求: Python >= 3.11

# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装
pip install -e .

# 3. 配置
cp nanobee.yaml.example nanobee.yaml
# 编辑 nanobee.yaml，填入 API Key

# 4. 运行对话
nanobee run
# nanobee run -v       详细日志
# nanobee run -c /path/to/config.yaml
```

### Windows

```powershell
# 环境要求: Python >= 3.11（从 Microsoft Store 或 python.org 安装）

# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 3. 安装
pip install -e .

# 4. 配置
copy nanobee.yaml.example nanobee.yaml
# 编辑 nanobee.yaml，填入 API Key

# 5. 运行对话
nanobee run
```

> **提示**：Windows 用户也可以使用项目根目录的 `run.bat` 快捷启动脚本，自动完成虚拟环境创建、依赖安装和启动对话。

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

### 核心组件

| 组件 | 职责 |
|------|------|
| `NanobeeKernel` | 统一入口，管理插件生命周期、消息路由 |
| `AgentLoop` | 6 态状态机驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE） |
| `AgentRunner` | LLM 调用 + 工具执行迭代（含 Hook 调用、上下文治理、SSRF 拦截） |
| `ContextManager` | 多租户上下文隔离（每个用户独立目录） |
| `ContextPipeline` | System Prompt 构建（Soul → Skill → Memory → Rules 管线） |
| `PluginManager` | 插件扫描、加载、生命周期控制 |
| `EventBus` | 异步事件发布/订阅 |
| `SoulGuard` | 灵魂文件三层保护 |
| `LockManager` | 按用户粒度的 asyncio 并发锁 |
| `ContextRouter` | 多租户路由（`channel:chat_id` → `user_id`） |
| `ContextSandbox` | 沙箱拦截路径逃逸（`../` 检测） |
| `ToolCollector` | 工具白/黑名单双重过滤 |

### Pipeline 如何组装 System Prompt

```
[P0]  Soul 段     ← core.md Soul 节（框架内置）
[P28] 技能段       ← SkillStage：从 skills/ 目录读取用户技能文档
[P30] 记忆段       ← 遍历插件 contribute_to_prompt → stage="记忆"
[P40] Rules 段     ← core.md Rules 节 + 用户隔离铁律（框架内置）
```

每个段有内容才注入，无内容跳过。框架只做**拼装**，不做任何业务理解。

## 内置插件

| 插件 | 类型 | 状态 | 说明 |
|------|------|------|------|
| `channel_cli` | Channel | ✅ 完整 | 命令行交互通道 |
| `tool_echo` | Tool | ✅ 完整 | 回显测试工具 |
| `tool_fs` | Tool | ✅ 完整 | 文件系统工具（read_file, write_file, edit_file, list_dir），L1/L2 防御纵深 |
| `tool_shell` | Tool | ✅ 完整 | Shell 命令工具（execute_shell），含 deny 模式拦截危险命令 |
| `tool_web` | Tool | ✅ 完整 | Web 工具（web_search, web_fetch），含 HTML 清理、SSRF 保护 |
| `tool_cron` | Tool | ✅ 完整 | Cron 定时任务（add, list, remove） |
| `memory_file` | Memory | ✅ 完整 | JSONL 文件记忆存储，ADD-only 设计，关键词检索 + 时间衰减 |
| `audit_logger` | Audit | ✅ 完整 | 参考：on_message_completed 审计日志 |
| `channel_http` | Channel | 🚧 存根 | HTTP 通道（待实现） |

## 插件开发

插件只需继承 `NanobeePlugin`，覆盖需要的 Hook 方法：

```python
from nanobee.plugins.base import NanobeePlugin

class MyPlugin(NanobeePlugin):
    name = "my_plugin"
    version = "1.0.0"
    plugin_type = "echo"
    stage = "记忆"

    def contribute_to_prompt(self, context) -> str | None:
        """向 ## 记忆 段注入内容"""
        memory_file = context.base_dir / "memory.txt"
        if memory_file.exists():
            return memory_file.read_text(encoding="utf-8")
        return None
```

### 5 个 Hook 接口

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

# 运行全部测试（200+ 用例）
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
| `test_tool_fs.py` | 21 | 文件系统工具（read/write/edit/list） |
| `test_phase1_acceptance.py` | 9 | 多租户隔离验收 |
| `test_phase2_acceptance.py` | 17 | Hook 机制验收 |
| `test_phase3_acceptance.py` | 18 | 参考插件 + SkillStage 验收 |
| `test_skill.py` | 31 | Skill 数据模型/管理器 |
| 其他 | 40+ | 流式 Hook、钉钉文件、Channel、Sandbox 等 |

## 项目结构

```
nanobee/
├── agent/                 # Agent 核心引擎
│   ├── loop.py           # 6 态状态机消息循环（1100 行）
│   ├── runner.py         # LLM 调用 + 工具执行引擎（1380 行）
│   ├── hook.py           # 复合 Hook 管理器
│   ├── model_presets.py  # 模型预设切换
│   └── tools/            # 工具体系
│       ├── base.py       # Tool/Schema 抽象基类
│       ├── registry.py   # 工具注册中心
│       ├── schema.py     # 参数 Schema 定义
│       ├── mcp.py        # MCP 桥接（stdio/SSE/HTTP）
│       ├── skill_manager.py  # 技能管理工具
│       └── message.py    # 消息工具
├── builtin/              # 内置插件（8 个已实现）
│   ├── channel_cli/      # CLI 通道
│   ├── channel_http/     # HTTP 通道 🚧
│   ├── memory_file/      # JSONL 记忆存储
│   ├── audit_logger/     # 审计日志参考
│   ├── tool_fs/          # 文件系统工具
│   ├── tool_shell/       # Shell 命令工具
│   ├── tool_web/         # Web 工具
│   ├── tool_cron/        # Cron 定时任务
│   └── tool_echo/        # 回显测试
├── cli/                  # 命令行入口
│   ├── main.py           # Click 入口
│   ├── run.py            # run 命令（完整实现）
│   ├── plugin.py         # plugin 子命令 🚧 存根
│   └── hub.py            # hub 子命令 🚧 存根
├── config/               # 配置加载
├── kernel/               # 微内核核心（12 个模块）
│   ├── kernel.py         # NanobeeKernel
│   ├── plugin_manager.py # 插件管理器
│   ├── router.py         # 多租户路由
│   ├── sandbox.py        # 路径逃逸沙箱
│   ├── soul_guard.py     # 灵魂文件守卫
│   ├── lock_manager.py   # 按用户并发锁
│   ├── tool_collector.py # 工具过滤
│   ├── context_manager.py  # 上下文管理器
│   ├── context_pipeline.py # System Prompt 构建
│   ├── user_context.py   # 用户上下文
│   ├── event_bus.py      # 事件总线
│   └── core_parser.py    # core.md 解析
├── plugins/              # 插件接口定义
├── providers/            # LLM Provider（6 个实现 + 注册表）
├── security/             # 安全策略 🚧 空模块
└── utils/                # 工具函数
```

## LLM Provider 支持

Anthropic、OpenAI、Azure OpenAI、AWS Bedrock、GitHub Copilot、OpenAI 兼容接口（支持 DeepSeek、Gemini、智谱、通义千问等 30+ 模型）。

支持 FallbackProvider 链式降级、流式输出、Tool Calling。

## 对比 nanobot

nanobee 从 [nanobot](https://github.com/HKUDS/nanobot) 衍生开发，核心差异：

| 维度 | nanobot | nanobee |
|------|---------|---------|
| 架构哲学 | 大而全（内置记忆/梦境/人设） | 极简微内核（路由/隔离/拼装） |
| 记忆策略 | 框架内置多种算法 | 插件化（memory 接口） |
| 技能管理 | 框架内置 | 用户知识资产（SKILL.md） |
| 隔离机制 | 逻辑隔离 | 物理隔离 + 沙箱 + 锁 |
| 插件系统 | 有限扩展点 | 5 个 Hook 契约 + 完整生命周期 |
| 代码量 | 数万行 | 精简聚焦 |

## 被剥离 —— 留给插件生态

以下内容**不属于框架核心**，留给社区或应用层插件实现：

- 记忆截断策略（由 `memory-vector`、`memory-summarizer` 等插件实现）
- 梦境整理系统（由后台 Daemon 插件实现）
- 人设漂移检测（由 `drift-detector` 插件实现）
- Subagent 委派工具（由 `tool-subagent` 插件实现）
- 复杂的 facts.json / episodic.json 格式化
- HTTP/WebSocket 服务（由 `channel_http` 插件补全）

框架的责任是**画地为牢（隔离）**，插件的自由是**破土动工（注入）**。

## 开发

```bash
pip install -e ".[dev]"
pip install pre-commit mypy pytest-cov
python -m pytest tests/ -v
```

详细贡献指南请见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT License。基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT License）衍生开发。

原始项目版权：Copyright (c) 2025-present Xubin Ren and the nanobot contributors  
本项目版权：Copyright (c) 2026 nanobee contributors