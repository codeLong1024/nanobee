# nanobee 🐝

> 极简 AI Agent 框架 —— 继承自 [nanobot](https://github.com/HKUDS/nanobot)，面向核心基础设施的精简重构。

---

## 概述

**nanobee** 是从 [nanobot](https://github.com/HKUDS/nanobot) 衍生的精简版本，专注于保留 Agent 框架的核心能力，剥离了非本质的 WebUI、非核心 Channel 和 Tool 实现，保持最小可行产品状态以支持灵活扩展。

**核心设计理念**：所有能力以插件形式提供，框架本身仅提供容器和生命周期管理。

## 当前状态

当前版本：**v0.1.0** — MVP 核心功能已实现，可运行完整对话循环。

### 已完成

- [x] **内核核心**：`NanobeeKernel` 统一入口，管理插件生命周期、消息路由、灵魂文件保护
- [x] **Agent Loop**：完整状态机驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE）
- [x] **上下文管理**：`ContextManager` 多上下文隔离、`ContextPipeline` 系统提示词构建
- [x] **事件总线**：`EventBus` 异步事件发布/订阅机制
- [x] **灵魂守卫**：`SoulGuard` 核心文件完整性校验（SHA-256 哈希）
- [x] **插件管理器**：`PluginManager` 插件扫描、加载、启用/禁用生命周期
- [x] **插件接口**：7 种插件类型接口定义（Channel、Tool、Memory、Skill、Knowledge、Dream、Plugin 基类）
- [x] **LLM Provider 集成**：Anthropic、OpenAI、Azure、Bedrock、GitHub Copilot、OpenAI Compatible 等多提供商支持
- [x] **模型预设**：运行时模型切换、预设配置管理
- [x] **MCP 工具桥接**：MCP 服务器连接与工具注册
- [x] **CLI 命令**：`nanobee run`（对话模式）、`nanobee hub`（插件市场）、`nanobee plugin`（插件管理）
- [x] **配置文件**：YAML 配置加载、环境变量注入、自动发现 `nanobee.yaml`

### 内置插件

| 插件 | 类型 | 状态 | 说明 |
|------|------|------|------|
| `channel_cli` | Channel | ✅ 已完成 | 命令行交互通道，支持 `input()` 循环 |
| `tool_echo` | Tool | ✅ 已完成 | 回显测试工具 |
| `memory_file` | Memory | ✅ 已完成 | 基于 JSONL 文件的记忆存储 |
| `channel_http` | Channel | 🚧 存根 | HTTP 渠道插件（待完善） |
| `tool_fs` | Tool | 🚧 存根 | 文件系统工具（待实现） |
| `tool_shell` | Tool | 🚧 存根 | Shell 执行工具（待实现） |
| `tool_web` | Tool | 🚧 存根 | Web 搜索/抓取工具（待实现） |

### 待实现

- [ ] 插件自动发现与热加载
- [ ] `channel_http` 插件完善（HTTP API 通道）
- [ ] `tool_fs`、`tool_shell`、`tool_web` 插件实现
- [ ] 插件市场机制（`hub search/install`）
- [ ] 插件管理 CLI（`plugin create/enable/disable`）
- [ ] 上下文自动压缩（Compaction）
- [ ] 记忆注入管道（Memory Stage）
- [ ] 梦境调度器（Dream Scheduler）完整实现
- [ ] 并发测试与覆盖率报告

## 测试

项目包含完整的测试套件，覆盖内核、插件系统、上下文安全和端到端集成：

```bash
# 安装依赖
pip install -e ".[dev]"

# 运行测试
python -m pytest tests/ -v --tb=short

# 运行测试并查看覆盖率
pip install pytest-cov
python -m pytest tests/ -v --cov=nanobee --cov-report=term-missing
```

### 测试覆盖

| 测试模块 | 状态 | 用例数 |
|---------|------|-------|
| `test_kernel.py` | ✅ 全部通过 | 3 |
| `test_plugin_system.py` | ✅ 全部通过 | 10 |
| `test_context_security.py` | ✅ 全部通过 | 4 |
| `test_e2e.py` | ✅ 全部通过 | 4 |

## 目录结构

```
nanobee/
├── agent/               # Agent 核心引擎
│   ├── loop.py          # Agent 主循环（状态机驱动）
│   ├── runner.py        # Agent 执行器（LLM 调用 + 工具执行）
│   ├── hook.py          # 进度钩子接口
│   ├── model_presets.py # 模型预设管理
│   └── tools/           # 工具注册与 MCP 桥接
├── builtin/             # 内置插件
│   ├── channel_cli/     # CLI 渠道插件 ✅
│   ├── channel_http/    # HTTP 渠道插件 🚧
│   ├── memory_file/     # 文件存储记忆插件 ✅
│   ├── tool_echo/       # 回显测试工具 ✅
│   ├── tool_fs/         # 文件系统工具 🚧
│   ├── tool_shell/      # Shell 工具 🚧
│   └── tool_web/        # Web 工具 🚧
├── cli/                 # 命令行入口
│   ├── main.py          # 主 CLI
│   ├── hub.py           # 插件市场子命令
│   ├── plugin.py        # 插件管理子命令
│   └── run.py           # Agent 运行命令
├── config/              # 配置加载与 Schema
├── kernel/              # 核心内核
│   ├── __init__.py      # NanobeeKernel 统一入口
│   ├── context_manager.py  # 上下文管理器
│   ├── context_pipeline.py # 上下文处理管道
│   ├── core_parser.py     # CoreMD 解析器
│   ├── dream_scheduler.py # 梦境调度器
│   ├── event_bus.py       # 事件总线
│   ├── personality.py     # 人格指纹
│   ├── plugin_manager.py  # 插件管理器
│   └── soul_guard.py      # 灵魂守卫
├── plugins/             # 插件接口定义
│   ├── base.py          # NanobeePlugin 基类
│   ├── channel.py       # ChannelPlugin 接口
│   ├── dream.py         # DreamPlugin 接口
│   ├── knowledge.py     # KnowledgePlugin 接口
│   ├── memory.py        # MemoryPlugin 接口
│   ├── skill.py         # SkillPlugin 接口
│   └── tool.py          # ToolPlugin 接口
├── providers/           # LLM 提供商实现
│   ├── base.py          # LLMProvider 基类
│   ├── anthropic_provider.py
│   ├── openai_compat_provider.py
│   ├── azure_openai_provider.py
│   ├── bedrock_provider.py
│   ├── github_copilot_provider.py
│   ├── openai_codex_provider.py
│   ├── factory.py       # Provider 工厂
│   ├── registry.py      # Provider 注册表
│   └── fallback_provider.py
├── security/            # 安全策略（占位）
├── templates/           # 模板文件
└── utils/               # 工具函数
    ├── helpers.py       # 通用辅助函数
    ├── document.py      # 文档提取
    ├── image_generation_intent.py
    └── runtime.py
```

## 快速开始

### 安装

```bash
pip install -e .
```

### 配置

```bash
# 复制配置示例
cp nanobee.yaml.example nanobee.yaml

# 编辑配置文件，填入 API Key
nano nanobee.yaml
```

### 运行对话

```bash
# 自动发现当前目录下的 nanobee.yaml
nanobee run

# 指定配置文件
nanobee run -c /path/to/config.yaml

# 指定插件目录
nanobee run --plugin-dir /path/to/plugins

# 详细日志模式
nanobee run -v
```

### CLI 命令

```bash
# 查看帮助
nanobee --help
nanobee run --help

# 插件管理（部分功能待实现）
nanobee plugin list
nanobee plugin create my-plugin

# 插件市场（待实现）
nanobee hub search web
nanobee hub install web-search
```

## 架构概览

```
用户输入
    │
    ▼
ChannelPlugin (CLI/HTTP/...) ──▶ EventBus ──▶ NanobeeKernel
                                              │
                                    ┌─────────┼─────────┐
                                    ▼         ▼         ▼
                              PluginManager  ContextManager  SoulGuard
                                    │         ▼         │
                                    ▼    AgentLoop (状态机)   │
                              ToolRegistry    │            │
                                    │    ┌────┴────┐       │
                                    ▼    ▼         ▼       │
                              AgentRunner  LLM Provider   │
                                    │         │          │
                                    └─────────┴──────────┘
                                              │
                                              ▼
                                         用户响应
```

### 核心组件

1. **NanobeeKernel** — 统一入口，管理插件生命周期、消息路由、灵魂文件保护
2. **AgentLoop** — 状态机驱动的消息处理循环（RESTORE → BUILD → RUN → SAVE → RESPOND）
3. **AgentRunner** — LLM 调用与工具执行迭代循环
4. **ContextManager** — 多上下文隔离与消息持久化
5. **ContextPipeline** — 系统提示词构建管道
6. **PluginManager** — 插件扫描、加载、启用/禁用
7. **EventBus** — 异步事件发布/订阅
8. **SoulGuard** — 核心文件完整性校验

## 插件开发

### 创建工具插件

```python
from nanobee.plugins.tool import ToolPlugin

class MyToolPlugin(ToolPlugin):
    name = "tool_my_tool"
    version = "1.0.0"

    def get_tools(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": "my_tool",
                "description": "我的工具描述",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "输入参数"}
                    },
                    "required": ["input"],
                },
            }
        }]

    async def execute_tool(self, tool_name: str, **kwargs) -> any:
        if tool_name == "my_tool":
            return f"处理结果: {kwargs.get('input')}"
        raise ValueError(f"未知工具: {tool_name}")
```

### 创建通道插件

```python
from nanobee.plugins.channel import ChannelPlugin

class MyChannelPlugin(ChannelPlugin):
    name = "channel_my_channel"
    version = "1.0.0"

    async def start(self) -> None:
        # 启动通道连接
        pass

    async def stop(self) -> None:
        # 关闭通道连接
        pass

    async def send(self, message: str, **kwargs) -> None:
        # 发送消息到通道
        pass
```

## 开发

### 环境要求

- Python >= 3.11
- 推荐使用 `uv` 或 `pip` 进行包管理

### 贡献指南

详细贡献指南请查看 [CONTRIBUTING.md](CONTRIBUTING.md)，包括：

- 代码规范与最佳实践
- 测试指南与覆盖率要求
- 插件开发流程
- PR 提交流程
- 行为准则

## 许可证

nanobee 基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT License）衍生开发。
本项目的代码继承、修改和新增内容均遵循 MIT 许可证。

原始项目版权：Copyright (c) 2025-present Xubin Ren and the nanobot contributors
