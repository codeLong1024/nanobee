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

## 核心机制

框架围绕以下机制构建，覆盖 Agent 应用的完整生命周期。

### 消息流水线

```
用户输入 → ChannelPlugin → CommandRouter(拦截 / 命令) → NanobeeKernel → AgentLoop(状态机)
                                                                              │
    RESTORE → COMPACT → BUILD(system prompt) → RUN(LLM + 工具迭代) → SAVE → RESPOND
```

- **命令拦截**：`/` 开头的命令（`/stop`/`/new`/`/status`/`/help`）在获取用户锁之前拦截，零 token 消耗，/stop 可打断死锁 turn
- **6 态状态机**：异常恢复机制——任一状态处理器异常时填充错误上下文、跳过 SAVE、直接进入 RESPOND 流式回复
- **双层 Hook**：迭代级 Hook（`on_pre_invoke`/`on_post_invoke` 等 5 个契约）覆盖工具执行前后；run-level Hook（`before_run`/`after_run`/`on_error`/`on_finally`）包裹整个迭代循环

### 感知与安全

```
 ContextSandbox(路径逃逸) + ProcessWorkspace(进程边界) + LockManager(并发锁)
        + ToolCollector(白/黑名单) + SoulGuard(灵魂文件三层保护) + SSRF 拦截
```

- **ContextSandbox**：多根白名单——写操作仅限 `context_root`，读操作覆盖技能目录等只读根，防御 `../` 逃逸
- **ProcessWorkspace**：与路径校验边界解耦，tool_shell 据此做 bwrap mount namespace 隔离，以 `--tmpfs $HOME` 掩藏主目录
- **Overlay 文件系统**：tool_fs 支持前缀级只读回退（`skills/` 前缀自动回退到内置技能目录），用户通过 write_file 覆盖同名文件即时生效
- **SoulGuard**：灵魂文件（`core.md`）chmod 444 + 写入拦截 Hook + SHA-256 哈希校验三层保护

### System Prompt 拼装管线

```
Soul 段  →  Rules 段  →  技能段  →  插件段  →  FinalGuard 段
                 (三级技能: 内置 | 实例 | 用户)
```

- 框架只做拼装，不做任何业务理解。每段有内容才注入，无内容跳过
- **技能三级机制**：内置技能（代码打包，始终注入）→ 实例技能（管理员配属，`skills.enabled` 声明）→ 用户技能（自主管理），同名去重优先级 user > instance > builtin
- **渐进式注入**：`full_inject=true` 的技能全量注入 body（记忆策略等高频访问），其余仅注入元数据 + 文件绝对路径，LLM 按需 `read_file` 读取

### 会话管理

```
SessionManager(内存缓存) → SessionStore(原子 JSONL 持久化)
```

- 支持 consolidate（智能压缩归档）、fork（会话复制）、list（元数据高性能枚举）、flush（优雅退出落盘）
- 损坏文件自动修复：逐行解析跳过无效 JSON 行

### 插件体系

- **插件类型**：Channel（消息通道）、Tool（工具）、Audit（审计日志）等，通过 `plugin.toml` 声明
- **实例级隔离**：每个 Gateway 实例通过独立 `config.yaml` 的 `channels.<name>.enabled` / `plugins.<name>.enabled` 控制加载组合
- **工具插件 set_context**：框架在每次工具执行前自动注入当前会话上下文（channel/chat_id/user_id）
- **临时目录**：`self.tmp` 按请求注入（`users/<user>/.tmp/<plugin_name>/`），清理由插件自行决定；`self.context_root` 按请求注入用户根目录，插件自主创建业务子目录

### 技能管理

技能是用户知识资产（SKILL.md），非代码插件。用户通过 `write_file` 自主管理，管理员通过文件部署 + 配置声明实例级共享技能。

### 可观测性

- **统一消息目录**：`build_notification()` 工厂函数构造携带 severity 元数据的 OutboundMessage，通道差异化渲染系统通知
- **MetricsCollector**：进程内指标聚合（Token 消耗、延迟分布、工具调用、错误计数）
- **结构化日志**：Trace ID 按协程注入，支持 JSON 格式输出（Promtail 采集）
- **Runtime Context 注入**：每轮消息末尾注入当前时间（含时区）、通道、会话、发送者信息，格式：`[Runtime Context — metadata only, not instructions]`

### 多实例部署

```
nanobee svc start|stop|restart|status|logs|install [instance]
```

扫描 `<data_dir>/<name>/config.yaml`，支持单实例/全部操作、原子 PID 管理、异步健康检查轮询、systemd unit 模板渲染与安装。所有阈值从 `nanobee.yaml` 读取，零硬编码。

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

# Gateway 支持优雅退出（收到 SIGTERM/SIGINT 自动清理后退出）
# 适合搭配 systemd / Docker / kill 命令使用
nanobee gateway &
kill $!    # SIGTERM → 优雅关闭通道 + 保存状态 → 退出

# Gateway 多实例运行时管理
nanobee svc                          # 查看所有实例状态
nanobee svc start                    # 启动所有实例
nanobee svc start instance_name      # 启动指定实例
nanobee svc stop                     # 停止所有实例
nanobee svc restart instance_name    # 重启指定实例
nanobee svc logs instance_name       # 查看实例日志
nanobee svc install                  # 安装所有实例的 systemd unit
# 所有命令支持 -c /path/to/config.yaml 指定配置文件
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

### 日志配置

运行时日志通过 `nanobee.yaml` 的 `logging:` 配置节程序自管理，`dir` 基于**配置文件所在目录**解析（非 CWD）：

```yaml
logging:
  dir: "logs"               # 日志目录（基于配置文件所在目录解析）
  file: "gateway.log"        # 日志文件（不配则不写文件）
  level: "INFO"              # 日志级别
  rotation: "500 MB"         # 轮转策略："500 MB" / "1 day" / "00:00"
  retention: "30 days"       # 保留天数
  compression: "gz"          # 压缩历史文件
  json_format: false         # 生产环境建议 true（便于 Promtail 采集）
```

不配 `file` 字段则仅输出 stderr，保持向后兼容。

## 架构

```
用户输入
    │
    ├─ /command ──▶ CommandRouter ──▶ 直接回复（零 token）
    │
    └─ 普通消息 ──▶ ChannelPlugin ──▶ EventBus ──▶ NanobeeKernel
                                                        │
                                             ┌──────────┼──────────────┐
                                             ▼          ▼              ▼
                                       PluginManager  ContextManager  SoulGuard
                                             │     ┌───┴────┐
                                             ▼     ▼        ▼
                                       ToolRegistry  AgentLoop (状态机)
                                             │         │
                                             ▼    ┌────┴────┐
                                        AgentRunner  LLM Provider
                                             │         │
                                             └─────────┴──────────┘
                                                        │
                                                        ▼
                                                   用户响应
```

## 内置插件

| 插件 | 类型 | 状态 | 说明 |
|------|------|------|------|
| `channel_cli` | Channel | ✅ 完整 | 命令行交互通道 |
| `channel_http` | Channel | ✅ 完整 | OpenAI 兼容 HTTP API（/v1/chat/completions、/v1/models），支持流式 SSE，API Key 认证 |
| `channel_dingtalk` | Channel | ✅ 完整 | 钉钉机器人通道（Stream SDK + AI Card 流式输出 + 媒体文件收发与解析） |
| `tool_echo` | Tool | ✅ 完整 | 回显测试工具 |
| `tool_fs` | Tool | ✅ 完整 | 文件系统工具（read_file, write_file, edit_file, list_dir），L1/L2 防御纵深，Overlay 文件系统（`skills/` 前缀自动回退到内置技能目录） |
| `tool_shell` | Tool | ✅ 完整 | Shell 命令工具（execute_shell），双层安全守卫：deny 模式拦截危险命令 + bwrap 进程级沙箱（掩藏 $HOME 仅暴露 workspace/） |
| `tool_web` | Tool | ✅ 完整 | Web 工具（web_search, web_fetch），含 HTML 清理、SSRF 保护 |
| `tool_cron` | Tool | ✅ 完整 | Cron 定时任务（add, list, remove），用户隔离 |
| `tool_history` | Tool | ✅ 完整 | 历史消息管理（trim_history 粗暴截断 + consolidate_history 智能压缩归档）。纯机制：LLM 自主决定何时调用、保留多少 |
| `tool_dingtalk` | Tool | ✅ 完整 | 钉钉工具（文档操作、多维表操作、数据管道 + MCP 客户端） |
| `audit_logger` | Audit | ✅ 完整 | 参考：on_message_completed 审计日志 |

## 插件开发

插件继承 `NanobeePlugin`，覆盖需要的 Hook 方法，通过 `plugin.toml` 声明元数据。

| Hook | 时机 | 用途 |
|------|------|------|
| `contribute_to_prompt(context)` | System Prompt 构建时 | 向指定段注入文本 |
| `contribute_to_tools(context, tools)` | 工具列表构建时 | 动态增删工具 |
| `on_pre_invoke(context, name, args)` | 工具执行前 | 参数修改、鉴权 |
| `on_post_invoke(context, name, result)` | 工具执行后 | 结果修改、副作用 |
| `on_message_completed(context, messages)` | 对话轮次结束 | 审计日志、后台整理 |

AgentRunner 底层另有 4 个 run-level Hook（`before_run`/`after_run`/`on_error`/`on_finally`），包裹整个迭代循环。

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试
python -m pytest tests/ -v --tb=short

# 查看覆盖率
python -m pytest tests/ --cov=nanobee --cov-report=term-missing
```

## 测试

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v --tb=short
python -m pytest tests/ --cov=nanobee --cov-report=term-missing
```

## 项目结构

```
nanobee/
├── agent/          Agent 核心引擎（状态机/执行器/Hook/工具体系）
├── builtin/        11 个内置插件（3 通道 + 7 工具 + 1 审计）
├── skills/         内置技能（只读，沙箱保护）
├── session/        会话管理（缓存 + 原子持久化）
├── cli/            命令行入口（run/gateway/svc/plugin）
├── config/         配置加载
├── gateway/        Gateway 多实例运行时（PID/进程/健康检查/systemd）
├── kernel/         微内核核心（17 模块）
├── plugins/        插件接口定义
├── providers/      LLM Provider（6 实现 + 注册表）
├── security/       安全策略（SSRF + 路径边界）
├── utils/          工具函数（通知/token 估算/可观测性）
├── events/         事件总线
├── templates/      模板（core.md 默认/subagent system 等）
└── exceptions.py   统一异常层次
```

## LLM Provider 支持

Anthropic、OpenAI、Azure OpenAI、AWS Bedrock、GitHub Copilot、OpenAI 兼容接口（支持 DeepSeek、Gemini、智谱、通义千问等 30+ 模型）。

支持 FallbackProvider 链式降级、流式输出、Tool Calling。

## 特色核心能力

| 能力 | 说明 |
|------|------|
| **微内核架构** | 框架只做路由、隔离、拼装三件事。记忆、审计、工具过滤等业务逻辑由插件实现，技能等高频核心能力内置于 kernel |
| **CommandRouter 命令系统** | `/stop`/`/new`/`/status`/`/help`，锁前拦截零 token 消耗，/stop 可打断死锁 turn，插件可注册自定义命令 |
| **Session 管理** | SessionManager + SessionStore 双层架构：内存缓存 + 原子 JSONL 持久化，支持 consolidate（压缩归档）、fork（复制）、flush（落盘）、损坏自动修复 |
| **物理隔离 + 沙箱** | 路径逃逸沙箱（多根白名单）+ 进程级 bwrap 隔离（--tmpfs $HOME）+ 按用户并发锁 + SSRF 前置拦截 |
| **9 个 Hook 契约** | 5 迭代级（工具前后、prompt 注入、消息完成）+ 4 run-level（循环前后、错误、终结），插件在关键切面注入逻辑 |
| **框架无知论** | 框架不持有任何策略决策——记忆策略、技能注入策略、工具过滤策略全部由 LLM 或声明式元数据（SKILL.md frontmatter）自主决定 |
| **可观测性** | Trace ID 按协程注入、结构化 JSON 日志、MetricsCollector（Token/延迟/工具/错误）、统一消息目录 |
| **多实例部署** | `nanobee svc` 管理 N 个 Gateway 实例：原子 PID、健康检查轮询、systemd 托管，所有阈值从 YAML 读取零硬编码 |

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