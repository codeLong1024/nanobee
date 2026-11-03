# MCP 工具规模化下的 Token 经济学：优化设计与 A2A 演进

> 状态：**设计方案，待方案评审**（遵守规则 19：框架核心改动先评审再动手）。本文不含代码改动。
>
> 定位：这是三大命题中「AI 模型与优化决策」的纵深篇——把"省 token"从经验技巧变成**可度量、可回滚的机制**，并给出 MCP 规模化与 A2A 的演进边界。

---

## 一、现状事实（先量，再谈优化）

### 1.1 工具定义是每轮全量下发的

```946:952:nanobee/agent/runner.py
        # 获取工具定义并应用过滤
        tool_definitions = spec.tools.get_definitions()
        if spec.filtered_tool_names is not None:
            allowed = set(spec.filtered_tool_names)
            tool_definitions = [
                d for d in tool_definitions
                if extract_tool_name(d) in allowed
            ]
```

`ToolRegistry.get_definitions()` 返回**注册表里的全部工具**（内置 + 全部 MCP tool/resource/prompt），排序后整体下发。每个 MCP 工具在注册时被包装成一个独立 `Tool`：

```345:355:nanobee/agent/tools/mcp.py
class MCPToolWrapper(_MCPWrapperBase):
    """Wraps a single MCP server tool as a nanobee Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout
```

即：**MCP 服务器暴露多少工具，每轮 prompt 就多背多少 JSON Schema**，且在一轮内的每次迭代（LLM 调用）都重复出现。

### 1.2 实测基线（本仓库，2026-10-19）

内置工具（`nanobee/builtin/tool_*`，共 14 个工具）：

| 插件 | 工具数 | 定义字符数 | 估算 token（len/2.5） |
|------|--------|-----------|----------------------|
| tool_task | 4 | 2388 | 955 |
| tool_fs | 5 | 1999 | 800 |
| tool_cron | 1 | 1762 | 705 |
| tool_history | 2 | 979 | 392 |
| tool_shell | 1 | 937 | 375 |
| tool_echo | 1 | 203 | 81 |
| **合计** | **14** | **8268** | **3307** |

推算（同一口径）：单个 MCP 工具定义典型 300–800 字符，一个 50 工具的企业级 MCP server ≈ 15–40 KB ≈ **6k–16k token/次请求**。若一轮对话迭代 5 次，仅工具定义就重复计费 5 次——**这部分成本与用户问题复杂度无关，是纯固定开销**。

### 1.3 已有的三块优化地基（不是从零开始）

| 已有能力 | 位置 | 意义 |
|----------|------|------|
| 工具定义缓存 + 稳定排序（内置在前、MCP 在后，各自按名排序） | `agent/tools/registry.py:91` | 顺序稳定 = 命中 prompt cache 的前提 |
| 缓存断点打在 **builtin/MCP 边界** 与列表尾部 | `providers/base.py:296` (`_tool_cache_marker_indices`) | MCP 工具抖动只影响尾段，内置段仍可命中缓存 |
| `cached_tokens` 跨 provider 归一（OpenAI `prompt_tokens_details`、DeepSeek `prompt_cache_hit_tokens`、Bedrock `cacheReadInputTokens`、Anthropic `cache_read_input_tokens`） | `providers/*_provider.py` | 缓存收益**可度量**，能进账本 |

这三块决定了优化方向：**不是无脑删 schema，而是"让稳定段更小、让动态段更少"**。

---

## 二、成本模型：工具定义为什么贵

一次请求的 prompt 由四段构成：

```
[系统提示 + 技能元数据]  [工具定义]  [历史消息]  [本轮输入]
        ↑ 稳定              ↑ 半稳定      ↑ 增长         ↑ 变化
```

- **稳定段**（系统提示）：缓存命中后价格通常是原价的 1/10，优化收益小、风险高，不动。
- **半稳定段**（工具定义）：内置稳定、MCP 随服务器能力膨胀——**这是唯一"纯增量、零业务价值增量"的成本**。
- **增长段**（历史消息）：已有 `max_messages`、`_snip_history`、工具结果落盘等机制。

因此结论明确：**优化主战场是工具定义，尤其是 MCP 工具定义**。

---

## 三、四层优化设计（按性价比排序）

### L0：缓存友好（已具备，需要"守住"）

优化不是只有"减字符"，还有"提高命中率"：

1. 工具顺序必须稳定：新增 MCP 工具不能重排内置段（现状已按名称排序，满足）。
2. 缓存断点位置：builtin/MCP 边界 + 尾部（现状已有）。这意味着**激活动态工具时应尽量"追加"而非"插入"**，避免移动既有断点。
3. 缓存命中必须可观测：`cached_tokens` 已归一，需在账本/审计中显式暴露 `cache_hit_ratio`，否则优化无法证伪。

> 反直觉但重要的一条：**动态工具列表每变一次，尾部缓存就 miss 一次**。所以"延迟加载"不能做成"每轮重算工具集"，否则省下的定义 token 会被缓存击穿抵消。

### L1：Schema 瘦身（低风险、收益确定）

MCP server 返回的 `inputSchema` 通常包含大量对模型无用的元数据：

| 可裁剪项 | 典型占比 | 风险 |
|----------|----------|------|
| `$schema` / `title` / `examples` / `default` | 5–15% | 极低 |
| 超长 `description`（部分 server 写整段文档） | 20–40% | 中（需保首句） |
| 深层嵌套 `anyOf`/`oneOf` 的空分支 | 5–10% | 低 |

设计要点：

- 裁剪规则**配置化**（每项开关 + 描述截断长度），框架只执行机制，不判断"这段描述重不重要"。
- 保留语义必需项：`type` / `properties` / `required` / `enum` / `x-constraint`（框架自有约束声明）。
- 裁剪前后做**调用成功率对比**（见第五节），不接受"看不出来差别"这种结论。

### L2：延迟加载（核心收益项）

范式（与 CodeBuddy 的 ToolSearch / DeferExecute 交互一致：主列表常驻 + 延迟工具按名或关键词检索 + 执行时装载）：

```
默认注入：  内置工具全量 + MCP 工具目录（name + 一行摘要）
                    ↓ LLM 判断需要
检索：      tool_search(query, limit) → 返回命中的完整 schema（limit 配置化）
                    ↓
激活：      命中的工具进入本轮/本会话激活集合
                    ↓
执行：      直接调用被激活工具
```

**关键设计决策（必须在评审时拍板）**：

| 决策 | 选项 | 建议 |
|------|------|------|
| 目录粒度 | 每工具一行 / 每 server 一行 | **每 server 一行 + 工具名列表**，目录体量从 O(工具数) 降到 O(server 数) |
| 激活生命周期 | 本轮 / 会话 / LRU 有界 | **会话级 + LRU 上界**（配置），避免每轮重复检索 |
| 未激活工具被直接调用 | 报错 / 自动激活 / 返回候选 | **自动激活并执行**（失败开放），同时返回"已自动加载"提示 |
| 检索实现 | 关键词 / BM25 / embedding | 首版纯**名称+描述关键词加权**（零依赖），embedding 作为可选后端 |
| 是否默认开启 | 全局开关 / 按 server | **按 server 开关**，小 server（<N 工具）不值得付检索成本 |

**与框架无知论的契合点**：框架只提供"目录 / 检索 / 装载"三个机制；"是否检索、检索什么、何时检索"全部由 LLM 决策；阈值、limit、开关全部来自配置。框架不硬编码任何 MCP 工具名或 server 名。

**实现落点（最小侵入）**：`filtered_tool_names` 已经是逐轮生效的工具可见性机制（`loop.py:1540` → `runner.py:947`），延迟加载可以复用它，不需要改 runner 的请求构造路径。

**收益估算**（以 50 工具 server 为例）：
- 现状：50 × ~500 字符 ≈ 25 KB ≈ 10k token/请求
- 目录化：50 个名字 + 一行摘要 ≈ 2.5 KB ≈ 1k token
- 命中 3–5 个工具：+1.5–2.5k token
- **单请求净省 60–75%**；缓存命中率不降反升（目录段稳定，激活段追加在尾部）。

### L3：结果侧（已有，补齐度量）

- 工具结果截断：`max_tool_result_chars`
- 大结果落盘引用：`maybe_persist_tool_result`（`read_file` 豁免，避免持久化死循环）
- 历史剪裁：`_snip_history`（含少量消息快速路径，避免 70 个工具定义重复编码）

这三项已在生产运行，缺的是**统一度量口径**（结果 token 占比、落盘命中率），否则无法判断下一步该优化哪一层。

---

## 四、MCP 连接侧优化（token 之外的"第二成本"）

连接不消耗 token，但消耗**首轮可用性与延迟**，直接影响"值不值"的体感。

**已具备（生产验证过的机制）**：

- 每 server 一个 owner task：anyio `CancelScope` 归属进入它的 task，跨 task 退出必崩——这条不是设计偏好，是结构性约束（`mcp_manager.py:76`）。
- `start()` 幂等等待：消除"首轮缺失 MCP 工具"的竞态（`mcp_manager.py:131`）。
- 失败冷却 `RETRY_COOLDOWN_S=30s`：挂死型 server 不会让每条消息都重付 30s 连接预算。
- 并行关闭 + 单 server 超时隔离：总关闭耗时不随 server 数线性放大。
- 重连预算 = 连接预算 + 拆解预算 + 余量：宁可等，也不假报失败。

**待优化项**：

| 问题 | 现状 | 建议 |
|------|------|------|
| 启动期连接阻塞 | gateway ready 路径内串行连接 | 并发预连接 + 总预算；未就绪 server 降级为"目录可见、工具暂不可用" |
| 工具目录与连接耦合 | 连接成功才注册工具，目录也随连接消失 | **目录与连接解耦**：目录可缓存到磁盘（带版本号），断连时仍可展示能力清单 |
| 注册原子性 | 重连时 unregister → 逐个 register | 先构建完整集合再原子替换，避免中间态被 LLM 看见半套工具 |
| 健康探测成本 | 依赖工具调用失败触发重连 | 保持"惰性探测"（不轮询），但把失败原因进账本，形成 server 级健康画像 |

---

## 五、度量与决策闭环（把"省了"变成"证明了"）

### 5.1 必采指标

| 指标 | 口径 | 用途 |
|------|------|------|
| `tool_tokens_per_request` | 工具定义段 token（按 provider 计数优先，tiktoken 兜底） | 优化主指标 |
| `cache_hit_ratio` | `cached_tokens / prompt_tokens` | 判断是否被缓存击穿 |
| `tool_search_hit_rate` | 检索后实际调用被激活工具的比例 | 判断目录是否可用 |
| `tool_call_success_rate` | 工具调用成功 / 总调用 | 护栏指标 |
| `first_turn_tool_availability` | 首轮请求中 MCP 工具可见比例 | 连接侧护栏 |
| `tokens_per_successful_task` | 总 token / 任务成功数 | 北极星 |

### 5.2 实验设计（A/B）

同一任务集（建议 30–50 条真实生产问法，覆盖单工具/多工具/无需工具三类），跑三组：

- A：现状全量下发
- B：L1 瘦身
- C：L1 + L2 延迟加载

对比：`tokens_per_successful_task`、`tool_call_success_rate`、平均迭代数、P95 延迟。
**放行条件**：token 显著下降且 `tool_call_success_rate` 不劣化；任一护栏恶化即回滚（配置级开关）。

### 5.3 与账本的关系

所有指标都应是 `TurnLedger` / `TurnReport` 的派生量，而不是另起一套统计——否则口径必然漂移（audit v2 的教训）。

---

## 六、A2A：扩展边界与前置条件

**定位分层**：

```
MCP  = Agent → Tool    （用能力）
A2A  = Agent → Agent   （找能力）
```

两者不重叠：MCP 解决"这个工具怎么调"，A2A 解决"这件事该找哪个 agent、它能不能接、结果怎么回"。

**nanobee 的落点（复用现有扩展点，不改内核）**：

| 方向 | 形态 | 复用机制 |
|------|------|----------|
| 入站 | A2A Channel 插件（Agent Card + 任务端点） | `ChannelPlugin` 扩展点、路由映射、`channel_http` 的鉴权范式 |
| 出站 | `a2a_send` 工具插件（发现 → 委派 → 轮询/回调） | `ToolPlugin` 扩展点、cron 的异步任务管理 |
| 身份 | Agent Card 声明 skills/能力 | 技能三级机制（内置/实例/用户）天然是能力声明 |

**必须先解决的三件事（否则 A2A 是负债）**：

1. **租户边界**：跨 agent 传递的数据可能携带租户信息，必须在出站前做字段级白名单，而不是"整个 context 转发"。
2. **Trace 传播**：`traceparent` 已具备 W3C 编解码基础，A2A 调用必须延续同一 trace，否则跨 agent 取证断链。
3. **预算与幂等**：委派任务要有 token/时长预算与幂等键，否则一次递归委派就能烧穿成本。

**建议节奏**：不作为首期卖点。先把 MCP 目录化（L2）做成，A2A 的价值才能被度量——因为"找 agent"和"找工具"共用同一套**预算与归因基础设施**。

---

## 七、分期与验收

| 阶段 | 内容 | 验收 |
|------|------|------|
| P0 | 补齐度量：`tool_tokens_per_request`、`cache_hit_ratio` 进账本 | 能从审计行直接算出，不改口径 |
| P1 | L1 schema 瘦身（可配置） | A/B 中 token 降幅达标，成功率不劣化 |
| P2 | L2 目录化 + `tool_search` + 自动激活 | 50 工具 server 场景 token 降 60%+，首轮可用率不降 |
| P3 | 连接侧：预连接、目录解耦、原子注册 | 启动 P95 与首轮可用率改善 |
| P4 | A2A 最小闭环（入站 Card + 出站委派） | 跨实例任务成功 + trace 完整 |

**回滚策略**：每一层都是配置开关，按 server / 全局两级生效；任一护栏指标恶化，关闭开关即回到上一态（不需要回滚代码）。

---

## 八、风险清单

| 风险 | 说明 | 应对 |
|------|------|------|
| 检索不准导致多一轮 | LLM 找不到工具 → 多一轮检索 | 目录摘要质量优先；未激活直接调用可自动激活 |
| 缓存击穿 | 激活集合变化导致尾部断点移动 | 激活只追加、目录段前置、稳定排序 |
| schema 裁剪改变语义 | 描述被截断导致参数理解错误 | 可关闭 + 成功率护栏 + 按 server 灰度 |
| 指标口径漂移 | 插件各算一套 token | 只允许从账本派生 |
| A2A 递归委派 | agent 互相调用形成环 | 委派深度上限 + 幂等键（配置化） |
