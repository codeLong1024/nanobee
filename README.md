# nanobee 🐝

> 极简 AI Agent 微内核框架 —— 继承自 [nanobot](https://github.com/HKUDS/nanobot)，聚焦核心基础设施的精简重构。

---

## 核心理念

大多数 AI Agent 框架是"大而全"的——自带记忆策略、梦境调度、人设漂移检测，但你可能用不上，或者想用自己的方案。

**nanobee 反其道而行之**：框架聚焦核心基础设施——**路由（Routing）、隔离（Isolation）、拼装（Composition）**。业务逻辑（记忆、审计、工具过滤）通过插件实现，而技能管理（SkillManager）等高频使用的核心能力直接内置于框架内核。

| 原则 | 含义 |
|------|------|
| **核心与插件分离** | 高频核心能力（技能发现与注入）内置于 kernel，业务逻辑（审计、工具过滤）留给插件 |
| **框架无知论** | 框架不知道"记忆"是什么、不知道哪些技能该全量注入，只读 SKILL.md 的 `full_inject` 标记，按标记执行注入策略（全量 or 渐进）。策略决策由 LLM 自主完成 |
| **隔离绝对论** | P0 唯一生死线——物理隔离 + 沙箱拦截。哪怕所有插件崩溃，Alice 也绝无法越权读取 Bob 一字节文件 |
| **Hook 侵入论** | 在消息流转的所有关键切面留下接口，让插件劫持上下文、动态修改工具列表、监听生命周期 |

## 项目状态

**版本 v0.1.0** — 核心框架（微内核、Agent 引擎、LLM Provider、插件体系）已通过 **456 个单元测试**验证。

```
Kernel 内核     ████████████████████████████████  95%
Agent 引擎      ████████████████████████████████ 100%
LLM Providers   ████████████████████████████████ 100%
内置插件        ████████████████████████████████ 100%
CLI 命令        ████████████████████████████████  75%
测试覆盖        ████████████████████████████████ 100%
```

### 已完成功能

- **NanobeeKernel** — 统一入口，管理插件生命周期、消息路由、灵魂文件保护
- **多租户隔离内核** — LockManager 并发锁、UserContext 元数据、ContextRouter 路由、ContextSandbox 沙箱、ToolCollector 双重过滤
- **Agent 状态机** — 6 态驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND），支持流式输出、中轮注入、并发锁
- **Plugin Hook 机制** — 5 个核心契约接口（contribute_to_prompt/contribute_to_tools/on_pre_invoke/on_post_invoke/on_message_completed），插件可在关键切面注入逻辑

- **Run-level Hook 机制** — AgentRunner 外层生命周期：before_run / after_run / on_error / on_finally，包裹整个 LLM 迭代循环，支持启动初始化、完成汇总、错误记录、资源释放
- **技能管理** — SkillsLoader 双源发现（内置技能 `nanobee/builtin/skills/` + per-user 技能 `users/<user_id>/skills/`），SKILL.md frontmatter 驱动渐进/全量注入策略。内置 `_memory`（记忆策略，`full_inject: true`声明全量注入）、`skill-creator`（技能创建教程）
- **内置 Skill** — 框架打包 `nanobee/builtin/skills/`，只读不可覆盖。标记 `full_inject: true` 的技能全量注入 system prompt，普通技能仅注入元数据由 LLM 按需读取
- **Sandbox 相对路径统一** — L1（sandbox.py）/ L2（tool_fs）相对路径均基于 sandbox root 解析，skill 中 `memory/xxx` 类相对路径始终落在沙箱内
- **上下文管理** — 按用户物理隔离的目录结构（identity.yaml / .history/ / workspace/ / memory/ / .tmp/），框架通过 ContextVar 按请求向插件注入 `self.tmp`，插件自管清理
- **灵魂守卫** — 三层保护（chmod 444 + 写入拦截 Hook + SHA-256 哈希校验）
- **插件管理器** — 扫描、加载（含依赖拓扑排序）、启用/禁用/卸载生命周期
- **LLM Provider** — Anthropic、OpenAI、Azure、Bedrock、GitHub Copilot、OpenAI 兼容接口、30+ 模型规格注册
- **MCP 桥接** — 连接 MCP 服务器注册工具，支持 stdio / SSE / Streamable HTTP 三种传输协议
- **CLI 命令** — `nanobee run`（轻量级 Agent CLI 模式，支持 `-m` 单次消息和 `-s` 会话 ID）、`nanobee gateway`（完整服务栈，通道 + 健康端点）、`plugin list`/`create`/`enable`/`disable` 完整实现、`hub` 子命令🚧
- **CLI/Gateway 职责分离** — 借鉴 nanobot 设计哲学：`boot()` 只做核心启动，`boot_services()` 启动后台服务。`nanobee run` 轻量无后台，`nanobee gateway` 启动完整服务栈
- **max_iterations 配置化** — 支持从 `nanobee.yaml` 配置 LLM 最大对话循环次数（默认 10）
- **OpenAI 兼容 HTTP API** — `POST /v1/chat/completions`（SSE 流式 + JSON 非流式）、`GET /v1/models`、API Key 认证，支持 LobeChat 等第三方客户端连接
- **Pipeline 声明式注入** — SkillStage 由 SKILL.md frontmatter 的 `full_inject` 标记驱动：标记为 true 的技能全量注入 body，其余仅注入元数据。从根源杜绝注入攻击（恶意 body 不进入 system prompt）
- **安全模块** — SSRF 前置拦截（DNS 解析 + 私有 IP 校验 + IPv6-mapped IPv4 标准化）、CIDR 白名单、shell 命令内网 URL 检测（`contains_internal_url`）、路径边界工具函数（多 root 支持）

### 尚不完整的功能

| 模块 | 说明 | 优先级 |
|------|------|--------|
| `cli/hub.py` | `search/install/uninstall` 子命令仅有 echo 占位 | 低 |

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

# 轻量级 Agent CLI 模式（开发调试）
nanobee run
nanobee run -v                       # 详细日志
nanobee run -m "你好"                 # 单次消息模式
nanobee run -s "my-session" -m "hi"  # 指定会话 + 单次消息
nanobee run -c /path/to/config.yaml

# 完整 Gateway 服务栈模式（生产部署：通道 + 健康端点）
nanobee gateway
nanobee gateway --port 8080           # 带健康检查 HTTP 端点
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

# 轻量级 Agent CLI 模式
nanobee run
nanobee run -m "你好"                 # 单次消息

# 完整 Gateway 服务栈模式
nanobee gateway
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

### 两种运行模式

```
┌─────────────────────────────────────────────────────────┐
│ nanobee run         轻量 Agent CLI 模式                  │
│                     └→ Kernel.boot() 核心启动            │
│                        (无通道/健康端点)                  │
├─────────────────────────────────────────────────────────┤
│ nanobee gateway     Gateway 完整服务栈                   │
│                     └→ Kernel.boot() + boot_services()   │
│                        (通道插件 + 健康端点)              │
└─────────────────────────────────────────────────────────┘
```

### 核心组件

| 组件 | 职责 |
|------|------|
| `NanobeeKernel` | 统一入口，管理插件生命周期、消息路由 |
| `AgentLoop` | 6 态状态机驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE） |
| `AgentRunner` | LLM 调用 + 工具执行迭代（含迭代级/run-level 双层 Hook、上下文治理、SSRF 拦截） |
| `ContextManager` | 多租户上下文隔离（每个用户独立目录：history.jsonl / work / memory / tmp） |
| `ContextPipeline` | System Prompt 构建（Soul → Rules → Skill → Memory 管线 + FinalGuard） |
| `PluginManager` | 插件扫描、加载、生命周期控制 |
| `SkillsLoader` | 技能双源发现（内置技能 + 用户技能），mtime 文件系统缓存（TTL 2 秒），去中心化：用户通过 write_file 自主管理 SKILL.md |
| `EventBus` | 异步事件发布/订阅 |
| `SoulGuard` | 灵魂文件三层保护 |
| `LockManager` | 按用户粒度的 asyncio 并发锁 |
| `ContextRouter` | 多租户路由（`channel:chat_id` → `user_id`） |
| `ContextSandbox` | 沙箱拦截路径逃逸（`../` 检测） |
| `context_sandbox_var` | ContextVar 注入：bind_sandbox/bind_tmp/reset_tmp/current_tmp |
| `ToolCollector` | 工具白/黑名单双重过滤 |

### Pipeline 如何组装 System Prompt

```
[P10] Soul 段         ← core.md Soul 节（框架内置）
[P20] Rules 段         ← core.md Rules 节 + 用户身份（框架内置）
[P28] 技能段           ← SkillStage：从 builtin/skills/ + users/<user_id>/skills/ 读取技能文档
                        · full_inject=true：全量注入 body（如 _memory 记忆策略）
                        · full_inject=false：渐进式注入（仅 name/description 元数据）
                        · 同名技能双方都展示，标注 [builtin] / [user] 来源
[P??] 插件段           ← 遍历插件 contribute_to_prompt → 按 plugin_type 生成段标题
                        · 由插件 stage/plugin_type 决定，不做固定顺序
[P90] FinalGuard 段    ← 不可绕过的优先级规则段（始终在最后）
```

每个段有内容才注入，无内容跳过。框架只做**拼装**，不做任何业务理解。

### 工具插件 set_context 机制

工具插件可通过 `set_context(channel, chat_id, user_id)` 接收会话上下文。框架在每次工具执行前自动调用此方法（如果工具支持），使工具插件能获取当前会话信息（如钉钉通道的 chat_id、用户 ID）。

### 插件临时目录（self.tmp）

框架在 `users/<user>/.tmp/` 下为每个用户创建临时目录，通过 ContextVar 按请求注入到插件。插件通过 `self.tmp` 获取自己的专属临时目录（`.tmp/<plugin_name>/`）：

```python
class MyPlugin(ToolPlugin):
    async def execute_tool(self, tool_name, **kwargs):
        # self.tmp 按请求自动注入，路径：.../users/<user>/.tmp/my_plugin/
        cache_file = self.tmp / "cache.json"
        # 框架只管创建和注入，清理由插件自行决定
```

- **框架做的事情**：创建目录、按请求注入 ContextVar、确保 `tmp/` 在沙箱边界内
- **框架不做的**：不清理 tmp 目录、不关心里面放什么子目录、不定义回收策略
- **插件决定**：何时清理、创建什么子目录、怎么用这个空间

## 内置插件

| 插件 | 类型 | 状态 | 说明 |
|------|------|------|------|
| `channel_cli` | Channel | ✅ 完整 | 命令行交互通道 |
| `channel_http` | Channel | ✅ 完整 | OpenAI 兼容 HTTP API（/v1/chat/completions、/v1/models），支持流式 SSE，API Key 认证 |
| `channel_dingtalk` | Channel | ✅ 完整 | 钉钉机器人通道（Stream SDK + AI Card 流式输出 + 媒体文件收发与解析） |
| `tool_echo` | Tool | ✅ 完整 | 回显测试工具 |
| `tool_fs` | Tool | ✅ 完整 | 文件系统工具（read_file, write_file, edit_file, list_dir），L1/L2 防御纵深 |
| `tool_shell` | Tool | ✅ 完整 | Shell 命令工具（execute_shell），含 deny 模式拦截危险命令 |
| `tool_web` | Tool | ✅ 完整 | Web 工具（web_search, web_fetch），含 HTML 清理、SSRF 保护 |
| `tool_cron` | Tool | ✅ 完整 | Cron 定时任务（add, list, remove） |
| `tool_dingtalk` | Tool | ✅ 完整 | 钉钉工具（文档操作、多维表操作、数据管道 + MCP 客户端） |
| `audit_logger` | Audit | ✅ 完整 | 参考：on_message_completed 审计日志 |

## 插件开发

插件只需继承 `NanobeePlugin`，覆盖需要的 Hook 方法：

```python
from nanobee.plugins.base import NanobeePlugin
from nanobee.utils.logger import logger

class MyPlugin(NanobeePlugin):
    name = "my_plugin"
    version = "1.0.0"
    plugin_type = "echo"
    stage = "记忆"

    def on_load(self) -> None:
        # self.tmp 在请求上下文中可用，路径: users/<user>/.tmp/my_plugin/
        # 框架只管创建目录，清理由插件自己决定
        logger.info("tmp 目录: {}", self.tmp)

    def contribute_to_prompt(self, context) -> str | None:
        """向 ## 记忆 段注入内容"""
        memory_file = context.base_dir / "memory.txt"
        if memory_file.exists():
            return memory_file.read_text(encoding="utf-8")
        return None
```

### 5 个 Plugin Hook 接口

| Hook | 时机 | 用途 |
|------|------|------|
| `contribute_to_prompt(context)` | System Prompt 构建时 | 向指定段注入文本 |
| `contribute_to_tools(context, tools)` | 工具列表构建时 | 动态增删工具 |
| `on_pre_invoke(context, name, args)` | 工具执行前 | 参数修改、鉴权 |
| `on_post_invoke(context, name, result)` | 工具执行后 | 结果修改、副作用 |
| `on_message_completed(context, messages)` | 对话轮次结束 | 审计日志、后台整理 |

### 4 个 Run-level Hook 接口（AgentRunner 底层）

| Hook | 时机 | 用途 |
|------|------|------|
| `before_run(context)` | 迭代循环开始前 | 启动计时、初始化计数器 |
| `after_run(context)` | 迭代循环正常结束后 | 总 token 消耗记录、汇总统计 |
| `on_error(context)` | 迭代循环因异常/业务错误终止时 | 错误诊断信息采集 |
| `on_finally(context)` | 无论正常/异常/取消始终调用 | 释放临时资源 |

> Run-level Hook 通过 `AgentRunHookContext` 传递运行级快照（messages、final_content、tools_used、usage、stop_reason、error、tool_events、exception），
> 与迭代级的 `AgentHookContext` 完全分离。现有自定义 Hook 子类不受影响（新方法默认 pass）。

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试（456 个用例）
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
| `test_user_context.py` | 15 | 用户上下文/元数据/tmp 注入 |
| `test_router.py` | 12 | 路由解析/降级/自定义 |
| `test_sandbox.py` | 16 | 路径逃逸拦截/边界 |
| `test_tool_collector.py` | 11 | 白/黑名单过滤 |
| `test_tool_fs.py` | 21 | 文件系统工具（read/write/edit/list） |
| `test_phase1_acceptance.py` | 9 | 多租户隔离验收 |
| `test_phase2_acceptance.py` | 17 | Hook 机制验收 |
| `test_phase3_acceptance.py` | 9 | 参考插件 + SkillStage 验收（含内置 skill 注入） |
| `test_channel_http.py` | 29 | HTTP API 通道（消息解析/SSE/认证/端点/错误处理） |
| `test_skill.py` | 25 | SkillsLoader 双源发现/缓存/序列化/向后兼容 |
| `test_skill_validator.py` | 14 | 技能校验器（name 格式、meta 完整性、白名单检查） |
| `test_skill_injection.py` | 7 | 渐进式注入防御 + full_inject 全量注入 + 内置 skill 标注 |
| `test_skill_manager_cache.py` | 7 | SkillsLoader 双源缓存性能验证 |
| `test_exceptions.py` | 24 | 统一异常层次结构（NanobeeError 子类、catch-all、模块导出、向后兼容） |
| `test_security.py` | 61 | SSRF 防护、CIDR 白名单、内网 URL 检测、路径边界工具 |
| `test_cli_plugin.py` | 23 | 插件发现/列表/创建/启用/禁用 CLI 命令 |
| `test_tool_dingtalk.py` | 42 | 钉钉插件（文档/多维表/管道 + MCP 客户端 + CSV 解析） |
| `test_runtime_context.py` | 12 | Runtime Context 时间/通道/会话信息注入 |
| `test_stream_hook.py` | 8 | 流式输出 Hook 链路 |
| `test_tool_cron_isolation.py` | 9 | Cron 定时任务用户隔离 |
| `test_tool_shell_sandbox.py` | 13 | Shell 沙箱路径逃逸拦截 |
| 其他 | 84+ | Run-level Hook、用户上下文、异常层次、钉钉文件等 |

## 项目结构

```
nanobee/
├── agent/                 # Agent 核心引擎
│   ├── loop.py           # 6 态状态机消息循环
│   ├── runner.py         # LLM 调用 + 工具执行引擎
│   ├── hook.py           # 复合 Hook + Run-level Hook（AgentRunHookContext / before_run / after_run / on_error / on_finally）
│   ├── model_presets.py  # 模型预设切换
│   └── tools/            # 工具体系
│       ├── base.py       # Tool/Schema 抽象基类
│       ├── registry.py   # 工具注册中心
│       ├── schema.py     # 参数 Schema 定义
│       ├── mcp.py        # MCP 桥接（stdio/SSE/HTTP）
│       ├── skill_manager.py  # 技能管理工具（ListSkillsTool + 自主文件管理）
│       └── message.py    # 消息工具
├── builtin/              # 内置插件（10 个已实现） + 内置技能（2 个）
│   ├── skills/           # 内置技能目录（只读，框架打包）
│   │   ├── _memory/SKILL.md    # 兜底记忆策略（full_inject: true 声明全量注入）
│   │   └── skill-creator/SKILL.md  # 技能创建教程
│   ├── channel_cli/      # CLI 通道
│   ├── channel_http/     # OpenAI 兼容 HTTP API
│   ├── channel_dingtalk/ # 钉钉机器人通道（Stream SDK + 媒体收发）
│   ├── audit_logger/     # 审计日志参考
│   ├── tool_dingtalk/    # 钉钉工具（文档/多维表/管道 + MCP 客户端）
│   ├── tool_fs/          # 文件系统工具
│   ├── tool_shell/       # Shell 命令工具
│   ├── tool_web/         # Web 工具
│   ├── tool_cron/        # Cron 定时任务
│   └── tool_echo/        # 回显测试
├── exceptions.py          # 统一异常层次结构（NanobeeError 基类 + 20 个子类）
├── cli/                  # 命令行入口
│   ├── main.py           # Click 入口
│   ├── run.py            # run 命令（轻量 Agent CLI，-m/-s 支持）
│   ├── gateway.py        # gateway 命令（完整服务栈）
│   ├── plugin.py         # plugin 子命令（list/create/enable/disable）
│   └── hub.py            # hub 子命令 🚧 存根
├── config/               # 配置加载
├── kernel/               # 微内核核心（15 个模块）
│   ├── kernel.py         # NanobeeKernel
│   ├── plugin_manager.py # 插件管理器
│   ├── skill_manager.py  # 技能加载器 SkillsLoader（双源发现 + mtime 缓存）
│   ├── skill_validator.py  # 技能校验器（name/description/属性白名单）
│   ├── router.py         # 多租户路由
│   ├── sandbox.py        # 路径逃逸沙箱
│   ├── soul_guard.py     # 灵魂文件守卫
│   ├── lock_manager.py   # 按用户并发锁
│   ├── tool_collector.py # 工具过滤
│   ├── context_manager.py  # 上下文管理器
│   ├── context_pipeline.py # System Prompt 构建（含 FinalGuardStage）
│   ├── user_context.py   # 用户上下文
│   ├── event_bus.py      # 事件总线
│   └── core_parser.py    # core.md 解析
├── plugins/              # 插件接口定义
├── providers/            # LLM Provider（6 个实现 + 注册表）
├── security/             # 安全策略（SSRF 防护 + 路径边界工具）
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
| CLI/Gateway 分离 | `agent` + `gateway` 双模式 | ✅ 已复刻：`run` 轻量 + `gateway` 完整服务栈 |
| 记忆策略 | 框架内置多种算法 | 内置 `_memory` Skill（LLM 自主管理），用户可覆盖自定义 |
| 技能管理 | 框架内置 CRUD | 文件驱动 SkillsLoader（builtin/ + users/<user_id>/skills/），用户通过 write_file 自主管理 |
| 隔离机制 | 逻辑隔离 | 物理隔离 + 沙箱 + 锁 |
| 插件系统 | 有限扩展点 | 5 个 Hook 契约 + 完整生命周期 |
| 代码量 | 数万行 | 精简聚焦 |

## 被剥离 —— 留给插件生态

以下内容**不属于框架核心**，留给社区或应用层插件实现：

- 高级记忆策略（向量检索、语义聚类等，由 `memory-vector` 等插件实现）
- 梦境整理系统（由后台 Daemon 插件实现）
- 人设漂移检测（由 `drift-detector` 插件实现）
- Subagent 委派工具（由 `tool-subagent` 插件实现）
- 复杂的 facts.json / episodic.json 格式化
- WebSocket 服务（由 `channel_ws` 插件实现）

框架的责任是**画地为牢（隔离）**——`.tmp/`目录、`ContextSandbox`沙箱、多租户上下文，都是给插件安全可靠的地盘。
插件的自由是**破土动工（注入）**——在这个地盘里，怎么写记忆、怎么管临时文件、怎么定义工具，全部由插件决定。

## 开发

```bash
pip install -e ".[dev]"
pip install pytest-cov
python -m pytest tests/ -v
```

详细贡献指南请见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT License。基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT License）衍生开发。

原始项目版权：Copyright (c) 2025-present Xubin Ren and the nanobot contributors  
本项目版权：Copyright (c) 2026 nanobee contributors