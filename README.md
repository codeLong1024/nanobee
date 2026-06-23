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

**版本 v0.1.0** — 核心框架（微内核、Agent 引擎、LLM Provider、插件体系、会话管理）已通过 **649 个单元测试**验证（1 skipped）。

```
Kernel 内核     ████████████████████████████████  95%
Agent 引擎      ████████████████████████████████ 100%
LLM Providers   ████████████████████████████████ 100%
内置插件        ████████████████████████████████ 100%
CLI 命令        ████████████████████████████████  85%
测试覆盖        ████████████████████████████████ 100%
```

### 已完成功能

#### 内核与隔离

- **NanobeeKernel** — 统一入口，管理插件生命周期、消息路由、灵魂文件保护。内置 **CommandRouter** 拦截 `/` 开头命令（`/stop`/`/new`/`/status`/`/help`），零 token 消耗。插件可通过 `kernel.command_router.register()` 注册自定义命令
- **多租户隔离内核** — LockManager 并发锁、UserContext 元数据、ContextRouter 路由、ContextSandbox 沙箱、ToolCollector 双重过滤
- **Agent 状态机** — 6 态驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND），支持流式输出、中轮注入、并发锁。**状态机层统一异常恢复**：任一状态处理器异常 → 填充错误上下文 → 跳过 SAVE（不污染历史）→ 直接进入 RESPOND 正常流式回复，kernel 层 catch 降级为最终兜底
- **Plugin Hook 机制** — 5 个核心契约接口（contribute_to_prompt/contribute_to_tools/on_pre_invoke/on_post_invoke/on_message_completed），插件可在关键切面注入逻辑

- **Run-level Hook 机制** — AgentRunner 外层生命周期：before_run / after_run / on_error / on_finally，包裹整个 LLM 迭代循环，支持启动初始化、完成汇总、错误记录、资源释放

#### 技能与知识

- **技能管理** — SkillsLoader 三级机制（内置 `nanobee/skills/` 始终注入 + 实例 `<data_dir>/skills/` 由 `skills.enabled` 声明 + 用户 `users/<user_id>/skills/` 始终注入），同名优先级 user > instance > builtin。部署方通过 `skills.enabled` 控制实例技能注入，同时自动推导为沙箱只读白名单 + bwrap 进程沙箱挂载点。SKILL.md frontmatter 驱动渐进/全量注入策略
- **4 个内置 Skill** — `_memory`（记忆策略，`full_inject: true`）、`skill_creator`（技能创建教程）、`cron`（定时任务指南）、`web-tools-guide-1.0.2`（Web 工具使用策略）。框架只读不可覆盖，沙箱只读根白名单保护。标记 `full_inject: true` 的技能全量注入 system prompt，普通技能仅注入元数据 + 文件绝对路径，由 LLM 按需读取
- **Pipeline 声明式注入** — SkillStage 由 SKILL.md frontmatter 的 `full_inject` 标记驱动：标记为 true 的技能全量注入 body，其余仅注入元数据。从根源杜绝注入攻击（恶意 body 不进入 system prompt）

#### 沙箱与安全

- **Sandbox 多根白名单** — ContextSandbox 从单根扩展为可读写根 + 只读根列表。写操作（write_file/edit_file）仅限 context_root 内，读操作（read_file/list_dir）可访问所有白名单根。内置技能目录自动加入只读白名单
- **Overlay 文件系统** — tool_fs 支持前缀级只读回退：LLM 读取 `skills/` 目录时，如果用户目录文件不存在，自动回退到内置技能目录。用户修改无需修改框架代码，通过 write_file 自主覆盖同名文件即生效。LLM 透明，零感知
- **ProcessWorkspace 进程级隔离** — 框架层定义"子进程可访问目录边界"(ProcessWorkspace)，与路径校验边界(ContextSandbox)解耦。tool_shell 通过 ContextVar 读取窄 workspace(=workspace/)，bwrap 以 `--tmpfs $HOME` 掩藏整个用户主目录，子进程仅暴露工作区目录
- **安全模块** — SSRF 前置拦截（DNS 解析 + 私有 IP 校验 + IPv6-mapped IPv4 标准化）、CIDR 白名单、shell 命令内网 URL 检测（`contains_internal_url`）、路径边界工具函数（多 root 支持）

#### 上下文与会话

- **上下文管理** — 按用户物理隔离的目录结构（identity.yaml / .history/ / workspace/ / memory/ / .tmp/），框架通过 ContextVar 按请求向插件注入 `self.tmp`（临时目录）和 `self.context_root`（用户根目录），插件自管清理和持久化子目录创建
- **Session 管理系统** — `SessionManager` + `SessionStore` 双层架构。内存缓存避免重复磁盘 I/O，原子 JSONL 写入保证数据一致性。支持 consolidate（智能压缩归档）、fork（会话复制）、list（元数据高性能枚举）、flush（优雅退出落盘）、旧版历史文件自动迁移。`consolidate_history` 工具归档早期对话并注入摘要 system 消息
- **灵魂守卫（SoulGuard）** — 三层保护（chmod 444 + 写入拦截 Hook + SHA-256 哈希校验）

#### 插件生态

- **插件管理器** — 扫描、加载（含依赖拓扑排序）、启用/禁用/卸载生命周期
- **11 个内置插件** — 3 通道 + 7 工具 + 1 审计（详见[内置插件](#内置插件)）
- **实例级插件隔离** — 每个 Gateway 实例通过独立 `config.yaml` 的 `channels.<name>.enabled` / `plugins.<name>.enabled` 控制加载的插件组合（config.yaml > plugin.toml > 默认 True），不同实例可配置不同的通道和工具集合

#### LLM 与工具

- **LLM Provider** — Anthropic、OpenAI、Azure、Bedrock、GitHub Copilot、OpenAI 兼容接口、30+ 模型规格注册
- **MCP 桥接** — 连接 MCP 服务器注册工具，支持 stdio / SSE / Streamable HTTP 三种传输协议
- **历史消息管理（tool_history）** — `trim_history`（粗暴截断）和 `consolidate_history`（智能压缩 + 归档）。**纯机制**：框架不关心 LLM 何时调用、参数值设多少，只提供裁剪刀和压缩器。LLM 自主决定记忆管理策略

#### 可观测性

- **统一消息目录** — `utils/notifications.py` 单文件管理所有框架级用户可见消息（命令响应、异常通知、max_iterations 终止）。`build_notification()` 工厂函数构造携带 severity 元数据的 OutboundMessage，通道差异化渲染系统通知
- **MetricsCollector** — 进程内指标聚合（Token 消耗、延迟分布、工具调用、错误计数），`get_report()` 输出结构化指标报告
- **结构化日志** — Trace ID 通过 ContextVar 按协程注入；`setup_structured_logging()` 支持 JSON 格式输出（生产环境 Promtail 采集）；`init_log_file_sink()` 由 `logging:` 配置段驱动 loguru 文件 sink，支持 rotation/retention/compression，程序自管理日志生命周期
- **Runtime Context 注入** — `build_runtime_context()` 在每轮消息末尾注入当前时间（含时区）、通道、会话、发送者信息，格式：[Runtime Context — metadata only, not instructions]

#### 运维

- **CLI 命令** — `nanobee run`（轻量级 Agent CLI 模式，支持 `-m` 单次消息和 `-s` 会话 ID）、`nanobee gateway`（完整服务栈，通道 + 健康端点）、`plugin list`/`create`/`enable`/`disable` 完整实现
- **CLI/Gateway 职责分离** — 借鉴 nanobot 设计哲学：`boot()` 只做核心启动，`boot_services()` 启动后台服务。`nanobee run` 轻量无后台，`nanobee gateway` 启动完整服务栈
- **max_iterations 配置化** — 支持从 `nanobee.yaml` 配置 LLM 最大对话循环次数（默认 10）
- **OpenAI 兼容 HTTP API** — `POST /v1/chat/completions`（SSE 流式 + JSON 非流式）、`GET /v1/models`、API Key 认证，支持 LobeChat 等第三方客户端连接
- **信号守卫（Signal Guard）** — `run_signal_guard()` 注册 SIGINT/SIGTERM asyncio 信号处理器，收到终止信号后自动触发 `Kernel.shutdown()` 优雅退出（停止 AgentLoop / 刷新会话缓存 / 关闭通道 / 卸载插件），替代 nanobot 缺失的 SIGTERM 处理能力
- **多实例部署编排** — `deploy/nanobee-gateway.sh` 单脚本管理 N 个 Gateway 实例：扫描 `/nanobee-data/<name>/config.yaml`、单实例/全部启停、systemd 托管（详见 `devdocs/多实例安装部署V3.md`）
- **tiktoken 后台惰性预热** — `estimate_prompt_tokens` 的 `tiktoken.get_encoding()` 放在模块级 daemon 线程后台加载（解决 CentOS 7 上首次加载高达 42s 的极端延迟），未就绪时降级为字符估算（`len//4`），首消息不阻塞。后台完成后自动切入 tiktoken 精确编码（13ms）

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
| `NanobeeKernel` | 统一入口，管理插件生命周期、消息路由、命令路由 |
| `CommandRouter` | Slash 命令系统（`/stop`/`/new`/`/status`/`/help`），零 token 消耗，**锁前拦截**：命令在获取用户锁之前执行，/stop 可打断死锁 turn，插件可注册自定义命令 |
| `SessionManager` | Session 生命周期管理（内存缓存 + 原子 JSONL 持久化），支持 consolidate（压缩归档）、fork（会话复制）、list（高性能枚举）、flush（优雅退出落盘）、旧版 `.history/` 自动迁移 |
| `SessionStore` | Session 文件存储层 — 纯 I/O 无缓存，临时文件 + `os.replace` 原子写入，JSON 损坏自动修复 |
| `AgentLoop` | 6 态状态机驱动循环（RESTORE → COMPACT → BUILD → RUN → SAVE → RESPOND → DONE） |
| `AgentRunner` | LLM 调用 + 工具执行迭代（含迭代级/run-level 双层 Hook、上下文治理、SSRF 拦截） |
| `TurnState 异常恢复` | 状态机层统一 catch：任一状态处理器异常 → 填充 ctx → 跳过 SAVE → 直接 RESPOND 流式回复。kernel 层 catch 降级为最终兜底，避免裸 OutboundMessage 丢失流式上下文 |
| `ContextManager` | 多租户上下文隔离（每个用户独立目录：history.jsonl / work / memory / tmp） |
| `ContextPipeline` | System Prompt 构建（Soul → Rules → Skill → Memory 管线 + FinalGuard） |
| `PluginManager` | 插件扫描、加载、生命周期控制 |
| `SkillsLoader` | 技能三级机制（内置 + 实例 + 用户），实例级由 skills.enabled 声明驱动，mtime 文件系统缓存（TTL 2 秒），去中心化：用户通过 write_file 自主管理 SKILL.md，管理员通过文件部署 + 配置声明实例级共享技能 |
| `EventBus` | 异步事件发布/订阅 |
| `SoulGuard` | 灵魂文件三层保护 |
| `MetricsCollector` | 进程内指标聚合（Token 消耗、延迟分布、工具调用、错误计数） |
| `LockManager` | 按用户粒度的 asyncio 并发锁 |
| `ContextRouter` | 多租户路由（`channel:chat_id` → `user_id`） |
| `ContextSandbox` | 多根沙箱 — 写操作仅限 context_root，读操作用于多根白名单（如内置技能只读目录），防御 `../` 逃逸。内置技能目录自动加入只读根，tool_fs 的 Overlay 文件系统在此基础上实现 `skills/` 前缀级别自动回退 |
| `ProcessWorkspace` | 子进程执行边界 — 与 ContextSandbox（路径校验边界）解耦，tool_shell 使用此边界做 bwrap mount namespace 隔离 |
| `context_sandbox_var` | ContextVar 注入：bind_sandbox/bind_tmp/bind_context_root/bind_process_workspace 及对应 reset/current 函数 |
| `ToolCollector` | 工具白/黑名单双重过滤 |

### Pipeline 如何组装 System Prompt

```
[P10] Soul 段         ← core.md Soul 节（框架内置）
[P20] Rules 段         ← core.md Rules 节 + 用户身份（框架内置）
[P28] 技能段           ← SkillStage：三级机制（框架提供，部署方声明）
                        · 内置技能（nanobee/skills/）：始终注入
                        · 实例技能（<data_dir>/skills/）：由 skills.enabled 配置白名单控制
                        · 用户技能（users/<user_id>/skills/）：始终注入
                        · full_inject=true：全量注入 body（如 _memory 记忆策略）
                        · full_inject=false：渐进式注入（仅 name/description 元数据 + 文件**绝对路径**，LLM 直接 read_file 读取正文）
                        · 同名技能标注 [builtin] / [instance] / [user] 来源，去重优先级 user > instance > builtin
                        · 启用实例技能的目录自动加入沙箱只读白名单 + bwrap --ro-bind-try
[P??] 插件段           ← 遍历插件 contribute_to_prompt → 按 plugin_type 生成段标题
                        · 由插件 stage/plugin_type 决定，不做固定顺序
[P90] FinalGuard 段    ← 不可绕过的优先级规则段（始终在最后）
```

每个段有内容才注入，无内容跳过。框架只做**拼装**，不做任何业务理解。

### 工具插件 set_context 机制

工具插件可通过 `set_context(channel, chat_id, user_id)` 接收会话上下文。框架在每次工具执行前自动调用此方法（如果工具支持），使工具插件能获取当前会话信息（如钉钉通道的 chat_id、用户 ID）。

### 插件临时目录（self.tmp）与用户根目录（self.context_root）

框架通过 ContextVar 按请求向插件注入两个目录：

**`self.tmp`** — 临时目录，`users/<user>/.tmp/<plugin_name>/`：
- 框架创建目录并按请求注入 ContextVar，清理由插件自行决定
- 适合缓存文件、中间结果等可丢弃数据

**`self.context_root`** — 用户根目录，`users/<user>/`：
- 框架只提供 basedir，**插件自己创建子目录**（符合框架无知论）
- 适合持久化数据，如 cron 任务、memory 存储等

**`process_workspace`** — 子进程可访问边界，`users/<user>/workspace/`：
- 框架层定义安全策略，与 ContextSandbox（路径校验边界）解耦
- tool_shell 据此做 bwrap mount namespace 隔离，以 `--tmpfs $HOME` 掩藏敏感配置
- 框架无知论：框架定义边界（机制），工具层只读标记执行（不决策）

```python
class MyPlugin(ToolPlugin):
    async def execute_tool(self, tool_name, **kwargs):
        # self.tmp — 临时数据
        cache_file = self.tmp / "cache.json"       # → users/<user>/.tmp/my_plugin/cache.json

        # self.context_root — 持久化数据（插件自己盖房子）
        store_dir = self.context_root / "my_data"   # → users/<user>/my_data/
        store_dir.mkdir(parents=True, exist_ok=True)
        store_file = store_dir / "data.json"
```

设计哲学：**框架给地，插件盖房子**。框架只管提供 basedir，业务子目录由插件自主创建。如 `tool_cron` 插件使用 `self.context_root / "cron" / "jobs.json"` 存储定时任务，`_memory` 技能使用 `memory/` 存储记忆。

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

# 运行全部测试（649 个用例）
python -m pytest tests/ -v --tb=short

# 查看覆盖率
python -m pytest tests/ --cov=nanobee --cov-report=term-missing
```

| 测试模块 | 用例数 | 覆盖场景 |
|---------|-------|---------|
| `test_security.py` | 61 | SSRF 防护、CIDR 白名单、内网 URL 检测、路径边界工具 |
| `test_tool_dingtalk.py` | 47 | 钉钉插件（文档/多维表/管道 + MCP 客户端 + CSV 解析） |
| `test_exceptions.py` | 40 | 统一异常层次结构（NanobeeError 子类、catch-all、模块导出、向后兼容） |
| `test_command_router.py` | 31 | Slash 命令系统（检测/路由/内置命令/自定义/错误处理/内核集成） |
| `test_tool_fs.py` | 30 | 文件系统工具（read/write/edit/list）+ Overlay 回退 |
| `test_channel_http.py` | 29 | HTTP API 通道（消息解析/SSE/认证/端点/错误处理） |
| `test_sandbox.py` | 29 | 路径逃逸拦截/多根白名单/写隔离 |
| `test_tool_shell_sandbox.py` | 28 | Shell 沙箱路径逃逸拦截 |
| `test_session.py` | 27 | Session 管理（CRUD/consolidation/fork/flush/缓存/旧版迁移/损坏修复） |
| `test_tool_history.py` | 25 | 历史消息管理（trim_history/consolidate_history/归档/摘要注入） |
| `test_skill.py` | 25 | SkillsLoader 双源发现/缓存/序列化/向后兼容 |
| `test_subagent.py` | 23 | 子代理创建与通信 |
| `test_stream_hook.py` | 23 | 流式输出 Hook 链路 |
| `test_cli_plugin.py` | 23 | 插件发现/列表/创建/启用/禁用 CLI 命令 |
| `test_phase2_acceptance.py` | 19 | Hook 机制验收 |
| `test_user_context.py` | 14 | 用户上下文/元数据/tmp 注入 |
| `test_token_usage_calibration.py` | 14 | Token 估算校准（OpenAI/DeepSeek/SDK 多格式提取、缓存检测） |
| `test_skill_validator.py` | 14 | 技能校验器（name 格式、meta 完整性、白名单检查） |
| `test_plugin_system.py` | 13 | 插件加载/生命周期/管理器 |
| `test_preset_manager.py` | 13 | 模型预设管理 |
| `test_mcp_manager.py` | 13 | MCP 服务器连接与工具注册 |
| `test_runtime_context.py` | 12 | Runtime Context 时间/通道/会话信息注入 |
| `test_router.py` | 12 | 路由解析/降级/自定义 |
| `test_tool_collector.py` | 11 | 白/黑名单过滤 |
| `test_phase3_acceptance.py` | 9 | 参考插件 + SkillStage 验收（含内置 skill 注入、绝对路径白名单） |
| `test_phase1_acceptance.py` | 9 | 多租户隔离验收 |
| `test_tool_cron_isolation.py` | 8 | Cron 定时任务用户隔离 |
| `test_dispatcher.py` | 8 | 事件分发 |
| `test_skill_injection.py` | 7 | 渐进式注入防御 + full_inject 全量注入 + 内置 skill 标注 |
| `test_skill_manager_cache.py` | 7 | SkillsLoader 双源缓存性能验证 |
| `test_process.py` | 6 | 信号守卫（SIGINT/SIGTERM 触发、重复信号防抖、mock kernel shutdown、降级兜底） |
| `test_plugin_enabled_override.py` | 6 | config.yaml enabled 覆盖 plugin.toml 的实例隔离 |
| `test_lock_manager.py` | 6 | 按用户并发锁隔离 |
| `test_context_security.py` | 4 | 上下文安全边界 |
| `test_kernel.py` | 3 | 内核初始化与基础操作 |
| `test_e2e.py` | 3 | 端到端集成 |

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
├── builtin/              # 内置插件（11 个已实现）
│   ├── channel_cli/      # CLI 通道
│   ├── channel_http/     # OpenAI 兼容 HTTP API
│   ├── channel_dingtalk/ # 钉钉机器人通道（Stream SDK + 媒体收发）
│   ├── audit_logger/     # 审计日志参考
│   ├── tool_dingtalk/    # 钉钉工具（文档/多维表/管道 + MCP 客户端）
│   ├── tool_fs/          # 文件系统工具
│   ├── tool_shell/       # Shell 命令工具
│   ├── tool_web/         # Web 工具
│   ├── tool_cron/        # Cron 定时任务
│   ├── tool_history/     # 历史裁剪工具
│   └── tool_echo/        # 回显测试
├── skills/               # 内置技能（只读，框架打包，沙箱只读根白名单）
│   ├── _memory/SKILL.md            # 兜底记忆策略（full_inject: true）
│   ├── skill_creator/SKILL.md      # 技能创建教程（渐进式注入，LLM 按需读取）
│   ├── cron/SKILL.md               # 定时任务指南（渐进式注入）
│   └── web-tools-guide-1.0.2/      # Web 工具使用策略（渐进式注入）
├── session/              # 会话管理系统
│   ├── session.py        # Session 数据模型
│   ├── session_manager.py# SessionManager：缓存层 + 业务编排（CRUD/fork/consolidate/list/flush）
│   ├── session_store.py  # SessionStore：文件 I/O 层（原子 JSONL 写入、损坏修复、旧版迁移）
│   └── __init__.py
├── exceptions.py          # 统一异常层次结构（NanobeeError 基类 + 20 个子类）
├── cli/                  # 命令行入口
│   ├── main.py           # Click 入口
│   ├── run.py            # run 命令（轻量 Agent CLI，-m/-s 支持）
│   ├── gateway.py        # gateway 命令（完整服务栈）
│   └── plugin.py         # plugin 子命令（list/create/enable/disable）
├── config/               # 配置加载
├── kernel/               # 微内核核心（17 个模块）
│   ├── kernel.py         # NanobeeKernel（含 CommandRouter 集成）
│   ├── command_router.py # Slash 命令路由系统 /stop/new/status/help + 插件注册
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
│   ├── core_parser.py    # core.md 解析
│   └── process.py        # 进程管理（信号守卫 → 优雅退出）
├── plugins/              # 插件接口定义
├── providers/            # LLM Provider（6 个实现 + 注册表）
├── security/             # 安全策略（SSRF 防护 + 路径边界工具）
└── utils/                # 工具函数
    ├── helpers.py        # 运行时上下文构建、token 估算
    ├── notifications.py  # 统一消息目录（build_notification / get_notification_content）
    └── observability.py  # Trace ID 注入、结构化日志、MetricsCollector
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
| 优雅退出 | 无 SIGTERM handler（`KeyboardInterrupt` 兜底） | ✅ `run_signal_guard()` 注册 SIGINT/SIGTERM asyncio 处理器，`Kernel.shutdown()` 完整清理 |
| 命令系统 | 无 | ✅ **CommandRouter** 零 token 消耗，/stop 打断死锁 turn，/new 会话重置，/status 运行时状态，/help 帮助，插件可注册自定义命令 |
| 会话管理 | 单文件 JSONL | ✅ **SessionManager/SessionStore** 内存缓存 + 原子持久化 + consolidate 归档 + fork 复制 + flush 落盘 + 自动损坏修复 + 旧版迁移 |
| 记忆策略 | 框架内置 Consolidator + AutoCompact + Dream Agent 三层，框架替 LLM 决定何时压缩/摘要 | ✅ 框架不持有记忆策略：安全阀 `history[-max_messages:]` 防崩溃，`_memory` skill 引导 + `consolidate_history`/`trim_history` 工具提供裁剪机制，LLM 自主管理，用户可覆盖 `skills/_memory/SKILL.md` 完全替换 |
| 技能管理 | 框架内置 CRUD | 文件驱动 SkillsLoader（builtin/ + instance/ + user/），同名优先级 user > instance > builtin，用户通过 write_file 自主管理，管理员通过文件部署 + 配置声明实例级共享技能 |
| 隔离机制 | 逻辑隔离 | 物理隔离 + 沙箱 + 锁 |
| 插件系统 | 有限扩展点 | 9 个 Hook 契约（5 迭代级 + 4 run-level） + 完整生命周期 |
| 可观测性 | 无 | ✅ 结构化日志（Trace ID + JSON 格式）+ MetricsCollector（Token/延迟/工具调用/错误）+ 统一消息目录 |
| 代码量 | 数万行 | 精简聚焦 |

## 被剥离 —— 留给插件生态

以下内容**不属于框架核心**，留给社区或应用层插件实现：

- 高级记忆策略（向量检索、语义聚类等，由 `memory-vector` 等插件实现）
- 自动压缩/摘要调度（框架不替 LLM 决定何时压缩，由 `_memory` skill + `trim_history` 工具 + LLM 自主完成）
- 梦境整理系统（nanobot 的 Dream Agent 已被 LLM 自主模式替代，详见 `devdocs/nanobot_vs_nanobee_memory_comparison.md`）
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