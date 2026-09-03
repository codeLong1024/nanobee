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
    RESTORE → BUILD(system prompt) → RUN(LLM + 工具迭代) → SAVE → RESPOND
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
nanobee svc start|stop|restart|status|logs [instance]
```

- 扫描 `<NANOBEE_DATA_DIR>/<name>/config.yaml`，支持单实例/全部操作
- 原子 PID 管理 + 异步健康检查轮询
- systemd 多实例单元通过环境变量注入 `NANOBEE_DATA_DIR`

**单实例 vs 多实例边界**：
- **单实例**（开发/测试/简单部署）：`nanobee gateway -c config.yaml`，不经过 svc
- **多实例**（同一主机跑 N 个 Gateway）：systemd 注入 `NANOBEE_DATA_DIR` 后由 svc 统一管理

详细运维操作见 [多实例部署运维手册](docs/multi_instance_ops.md)。

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
nanobee gateway -c /path/to/config.yaml

# Gateway 支持优雅退出（收到 SIGTERM/SIGINT 自动清理后退出）
# 适合搭配 systemd / Docker / kill 命令使用
nanobee gateway &
kill $!    # SIGTERM → 优雅关闭通道 + 保存状态 → 退出

# Gateway 多实例运行时管理
# 设置数据目录，svc 会扫描该目录下所有子目录的 config.yaml
export NANOBEE_DATA_DIR=~/.nanobee
nanobee svc status                    # 查看所有实例状态
nanobee svc start                     # 启动所有实例
nanobee svc start <instance-1>        # 启动指定实例
nanobee svc stop                      # 停止所有实例
nanobee svc restart <instance-1>      # 重启指定实例
nanobee svc logs <instance-1> -f      # 跟踪查看实例日志
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
| `tool_fs` | Tool | ✅ 完整 | 文件系统工具（read_file, write_file, edit_file, delete_file, list_dir），L1/L2 防御纵深，Overlay 文件系统（`skills/` 前缀自动回退到内置技能目录） |
| `tool_shell` | Tool | ✅ 完整 | Shell 命令工具（execute_shell），双层安全守卫：deny 模式拦截危险命令 + bwrap 进程级沙箱（掩藏 $HOME 仅暴露 workspace/） |
| `tool_web` | Tool | ✅ 完整 | Web 工具（web_search, web_fetch），含 HTML 清理、SSRF 保护 |
| `tool_cron` | Tool | ✅ 完整 | Cron 定时任务（add, list, remove），用户隔离 |
| `tool_history` | Tool | ✅ 完整 | 历史消息管理（trim_history 粗暴截断 + consolidate_history 智能压缩归档）。纯机制：LLM 自主决定何时调用、保留多少 |
| `audit_logger` | Audit | ✅ 完整 | 参考：turn/tool 两级 span 审计（JSONL + 结构化日志），预览截断带诚实标记，`preview_truncate` 开关支持全量记录 |

### 测试覆盖

项目共有 **1041 个测试用例**，覆盖核心模块：插件系统、Hook 机制、沙箱安全、消息路由、技能注入、故障分类等。
测试文件按模块组织（如 `test_xxx.py` 对应 `nanobee/xxx.py`），无分期命名的历史遗留文件。

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

# 运行全部测试（1041 用例，零回归）
python -m pytest tests/ -v --tb=short

# 查看覆盖率
python -m pytest tests/ --cov=nanobee --cov-report=term-missing
```

### 测试文件结构

| 文件 | 覆盖模块 | 用例 |
|------|---------|------|
| `test_audit_logger.py` | `builtin/audit_logger/plugin.py` — turn/tool 两级 span 结构化审计 | 36 |
| `test_channel_dingtalk.py` | `builtin/channel_dingtalk/` — 钉钉通道（流式卡片、媒体、限流） | 50 |
| `test_channel_http.py` | `builtin/channel_http/` — HTTP 通道 | 29 |
| `test_channel_manager.py` | `kernel/channel_manager.py` — 通道启停、优雅关闭 | 12 |
| `test_cli_plugin.py` | `cli/plugin.py` — CLI 插件命令 | 23 |
| `test_command_router.py` | `agent/command_router.py` — 命令路由 | 32 |
| `test_context_security.py` | `kernel/context_manager.py` — 路径安全、core.md 校验 | 4 |
| `test_e2e.py` | 端到端集成测试 | 3 |
| `test_error_card_finalization.py` | `agent/loop.py` + `builtin/channel_dingtalk/` — 错误卡片终态化 | 4 |
| `test_events.py` | `events/` — EventBus + RuntimeEventBus | 25 |
| `test_exceptions.py` | `agent/exceptions.py` — 异常体系 | 40 |
| `test_fault_classifier.py` | `agent/fault_classifier.py` — 故障分类 | 13 |
| `test_fresh_session.py` | `agent/loop.py` + `kernel/kernel.py` — 声明式无历史会话 | 9 |
| `test_gateway_runtime.py` | `gateway/` — Gateway 运行时 | 52 |
| `test_hook_scheduling.py` | `plugins/hook_mixin.py` — Hook 优先级调度 | 35 |
| `test_inject_message.py` | `kernel/kernel.py` — 统一消息注入 | 8 |
| `test_kernel.py` | `kernel/kernel.py` — 内核集成 | 3 |
| `test_lock_manager.py` | `kernel/lock_manager.py` — 并发锁、用户隔离 | 7 |
| `test_mcp_manager.py` | `agent/tools/mcp.py` — MCP 管理器 | 13 |
| `test_message_tool.py` | `agent/tools/message.py` — MessageTool | 21 |
| `test_notifications.py` | `utils/notifications.py` — 通知系统 | 19 |
| `test_plugin_blacklist.py` | `kernel/plugin_manager.py` — 插件禁用黑名单 | 2 |
| `test_plugin_config_schema.py` | `plugins/base.py` — config_cls 声明式配置 schema | 12 |
| `test_plugin_dirs.py` | `kernel/plugin_dirs.py` — 插件目录解析 | 10 |
| `test_plugin_enabled_override.py` | `kernel/plugin_manager.py` — enabled 配置覆盖 | 6 |
| `test_plugin_system.py` | `plugins/base.py` + `hook_mixin.py` — PluginManager、Hook 机制、ContextPipeline 集成 | 30 |
| `test_preset_manager.py` | `agent/preset_manager.py` — 预设管理 | 13 |
| `test_probe_http_url.py` | `agent/tools/mcp.py` — HTTP URL 可达性探测 | 4 |
| `test_process.py` | `kernel/process.py` — 进程管理 | 6 |
| `test_result_normalizer.py` | `agent/result_normalizer.py` — 结果标准化 | 10 |
| `test_router.py` | `kernel/context_router.py` — 规则解析、fallback、优先级 | 12 |
| `test_runner_error_streaming.py` | `agent/runner.py` — 流错误终态 | 2 |
| `test_runtime_context.py` | `utils/helpers.py` — 运行时上下文 | 12 |
| `test_sandbox.py` | `kernel/sandbox.py` — 路径安全、白名单、拦截 | 39 |
| `test_schema_validation.py` | `config/schema.py` — 配置校验 | 18 |
| `test_security.py` | `security/` — SSRF 防护、工作区策略 | 104 |
| `test_session.py` | `session/` — 会话管理 | 42 |
| `test_skill.py` | `kernel/skill_manager.py` — SkillsLoader 基础 | 25 |
| `test_skill_injection.py` | `kernel/skill_manager.py` — SkillStage 渐进注入、full_inject | 8 |
| `test_skill_manager_cache.py` | `kernel/skill_manager.py` — 缓存机制 | 7 |
| `test_skill_validator.py` | `kernel/skill_manager.py` — SKILL.md 格式校验 | 15 |
| `test_stream_hook.py` | `agent/hook.py` — StreamBridgeHook | 23 |
| `test_subagent.py` | `agent/subagent.py` — 子代理 | 23 |
| `test_token_usage_calibration.py` | `utils/helpers.py` — Token 估算 | 14 |
| `test_tool_collector.py` | `kernel/tool_collector.py` — 过滤、黑名单、白名单 | 12 |
| `test_tool_cron_error_notify.py` | `builtin/tool_cron/plugin.py` — 错误通知透传 | 13 |
| `test_tool_cron_isolation.py` | `builtin/tool_cron/service.py` — 用户隔离 | 10 |
| `test_tool_cron_redline.py` | `builtin/tool_cron/service.py` — 调度间隔安全红线 | 10 |
| `test_tool_fs.py` | `builtin/tool_fs/plugin.py` — 文件读写编辑删除 | 37 |
| `test_tool_history.py` | `agent/tools/tool_history.py` — 历史消息管理 | 25 |
| `test_tool_pipeline_logging.py` | `agent/tool_pipeline.py` — 工具管线日志 ctx 关联键 | 5 |
| `test_tool_shell_sandbox.py` | `builtin/tool_shell/sandbox.py` — bwrap 沙箱 | 39 |
| `test_user_context.py` | `kernel/user_context.py` — 会话创建、隔离、插件注入 | 15 |

## 项目结构

```
nanobee/
├── agent/          Agent 核心引擎（状态机/执行器/Hook/工具体系）
├── builtin/        10 个内置插件（3 通道 + 6 工具 + 1 审计）
├── channel/        通道抽象层（基类、消息处理）
├── cli/            命令行入口（run/gateway/svc）
├── config/         配置加载与校验（loader/schema/paths）
├── events/         事件总线（异步事件分发）
├── gateway/        Gateway 多实例运行时（PID/进程/健康检查/发现/日志）
├── kernel/         微内核核心
├── plugins/        插件接口定义
├── providers/      LLM Provider（6 后端实现 + 37 注册表 + Fallback/图像/语音）
├── security/       安全策略（SSRF + 路径边界）
├── session/        会话管理（SessionManager/Store/模型）
├── skills/         内置技能（只读，沙箱保护）
├── templates/      模板（core.md 默认/subagent system 等）
├── utils/          工具函数（通知/token 估算/可观测性）
├── __main__.py     允许 python -m nanobee 运行
├── bootstrap.py    组合根（配置→组件创建→AgentLoop 装配）
└── exceptions.py   统一异常层次
```

### 测试目录

```
tests/
├── test_lock_manager.py          # 并发锁、用户隔离
├── test_router.py                # 规则解析、fallback、优先级
├── test_sandbox.py               # 路径安全、白名单、拦截
├── test_tool_collector.py        # 过滤、黑名单、白名单
├── test_user_context.py          # 会话创建、隔离、插件注入
├── test_plugin_system.py         # PluginManager、Hook 机制、ContextPipeline
├── test_fault_classifier.py      # 故障分类（SSRF/沙箱/网络）
├── test_message_tool.py          # MessageTool、消息合并
├── test_notifications.py         # Notification 消息目录
├── test_audit_logger.py          # 审计日志
└── ...                           # 共 53 个测试文件，1041 用例
```

## LLM Provider 支持

Anthropic、OpenAI、Azure OpenAI、AWS Bedrock、GitHub Copilot、OpenAI Codex 及 OpenAI 兼容接口（内置注册表支持 DeepSeek、Gemini、智谱、通义千问、Moonshot、MiniMax、Mistral、StepFun 等 37 个 ProviderSpec）。

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
| **多实例部署** | `nanobee svc` 管理同一主机 N 个 Gateway 实例：原子 PID、健康检查轮询、systemd 托管。单实例直接 `nanobee gateway -c config.yaml`。详见[运维手册](docs/multi_instance_ops.md) |

## 许可证

MIT License。基于 [nanobot](https://github.com/HKUDS/nanobot)（MIT License）衍生开发。

原始项目版权：Copyright (c) 2025-present Xubin Ren and the nanobot contributors  
本项目版权：Copyright (c) 2026 nanobee contributors
