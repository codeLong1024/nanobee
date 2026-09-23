# 变更日志（Changelog）

本项目所有显著变更记录于此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Changed

- **可观测性**：turn 真值改为框架一级事实「TurnLedger 记账本 + TurnReport 结账单」，
  Hook 契约 `on_message_completed(context, messages)` 硬切为
  `on_message_completed(context, report)`，`on_message_started` 新增 `turn_id` 参数（破坏性变更，无向后兼容）。
  此前 runner 已算出完整 turn 事实（provider 精确 usage、逐轮 finish_reason、LLM 耗时、注入次数、退出原因），
  但只把 `result.messages`（有损的 LLM 上下文重建物）传给 completed Hook，其余全部丢弃，
  迫使插件启发式反推——"取最后一条 user 消息"导致排空（drain）注入后输入归因错位，
  "从全量历史数 tool_calls"导致历史轮次污染计数与二次刷写，"字符÷4 估 token"在
  provider 精确 usage 存在时完全失真。现 runner 在已计算事实的现成代码行旁 O(1) 追加
  `TurnLedger`（逐轮 `IterationFact`、逐次 `InjectionFact`、唯一 return 出口盖章），
  随 `AgentRunResult.ledger` 发布；loop 在 turn 边界盖章 `turn_id`（W3C trace id）
  与 `turn_started_at` 合成 `TurnReport`。机制归框架（只记账发布），策略归插件（决定记什么）。
  `audit_logger` 插件随之退化为账本→契约的纯映射（净删 5 个启发式函数），契约升
  `nanobee.audit/3`：`trace_id` 从 `turn_{uuid12}` 变为 W3C trace id（与日志流同源可串联）、
  token 变 provider 实测、`finish_reasons` 变账本原值（保序去重）、input/output messages
  记录本轮全部 user 输入（含注入）与 assistant 回复、新增 `nanobee.injections` /
  `nanobee.injected_messages` / `nanobee.exit_reason` / `nanobee.error`。
  turn 级时间三值（`start_time` / `end_time` / `duration_ms`）全部由 loop 盖章
  （`turn_started_at` / `turn_ended_at`），插件不再持有第二时钟；JSONL 契约
  维持「一行 = 一个终态 turn」，`record_type` 字段为未来 span 树中间层预留。
  新增 `tests/test_turn_ledger.py`（14 用例）与 `tests/test_runner_ledger.py`（12 用例）；
  `test_audit_logger.py` 全量改造为 v3 契约（52 用例）。

### Added

- **错误串统一收口 `utils/redact.py`（用户可见侧脱敏 + 形态归一）**：
  此前错误诊断串的 `Error:` 前缀时有时无（runner 兜底自拼 `f"Error: {type}: {exc}"`、
  provider 以 `content="Error: ..."` 承载错误、loop/kernel 兜底不带前缀，同一错误在三个
  出口三种形态），且脱敏只落在审计侧——`_state_respond` / `handle_message` 的 `detail`
  与 `metadata["error_detail"]` 仍把第三方异常原文裸传给用户（httpx 的 `HTTPStatusError`
  str 实测含 `for url '...?key=...'`，且该 metadata 会被 `tool_cron` 读走直接投递回原会话，
  构成第二条绕开通知模板的用户可见路径）。现新增叶子模块 `nanobee/utils/redact.py`：
  `normalize_error()` 把异常对象或已有文本归一为统一无前缀形态（`<异常类型>: <正文>`，
  `Error:` / `Exception:` 前缀统一剥离，provider content 与 runner/loop/kernel 兜底同形）
  并同时脱敏；`redact_secrets()` 供审计侧复用。runner 4 处、loop 1 处、kernel 2 处产生点
  全部改调，下游出口（通知 content、`metadata.error_detail`、审计 JSONL、运行日志）自动继承，
  未来新增出口无需再补——脱敏绑数据而非绑出口。正则顺带修正两处漏掩码缺陷：`\b` 前置边界
  在 `_` 两侧不成立致 `client_secret=` / `db_password=` 等 snake_case 键失配、JSON 形态
  `"api_key": "sk-x"` 因引号包裹失配。`audit_logger` 删除私有 `_ERROR_REDACT_PATTERN` /
  `_redact_error` 改复用公共实现，消除两侧规则漂移；新增 `tests/test_redact.py`（20 用例）。

- **评审修复批次（TurnLedger/观测体系 4 阻断项 + 2 建议项落地）**：
  ① **user_id 存储键双层防线（安全修复补全）**——原「`ContextManager.get_or_create`
  唯一守卫点」存在缺口：`_state_build` 先把未校验 user_id 缓存进
  `SessionManager._cache`，守卫后置触发，关停 `flush_all()` 经
  `SessionStore._session_path`（user_id 未净化）越界 `mkdir` + 写盘；HTTP 通道
  `conversation_id`（请求体可控）即攻击入口。现改为「出生点归一 + 落点断言」：
  新增叶子模块 `nanobee/utils/user_id.py`（`is_safe_user_id` / `resolve_storage_key`），
  `InboundMessage.context_id` 属性与 `_process_message` key 派生（存储键出生点）
  统一归一——合法 id 原样返回（既有用户目录零迁移），白名单外 id（钉钉加密形态
  `$:LWCP_v1:$...`、通道前缀 `dingtalk:cid...` 等）确定性降级为 `u-<sha256[:32]>`
  （同 raw 恒同 key，可用性优先于拒绝，替代原拒绝式校验对合法 id 的可用性破坏），
  `.`/`..`/非字符串仍拒绝；`SessionStore` / `audit_logger._jsonl_path` 拼路径前
  落点断言，恶意 id 连 `SessionManager` 缓存都进不去，越界写在读盘前即被拦截；
  ② **ABANDONED 审计落点修正**——`bind_context_root` 原仅在 `_state_run` 内绑定，
  兜底结账任务（`_process_message` finally）创建时 ContextVar 已复位，审计错误回退
  `/tmp/nanobee-audit`（可预测、跨实例共享、不防符号链接）。现把绑定提升到
  `_process_message` 外层（`ContextVar.reset(token)` 语义保证 `_state_run` 内层
  bind/reset 零改动兼容），取消/异常/关停路径的审计与正常路径落同一用户目录；
  兜底 `abandon_error` 优先采用 `ctx.error` 真实诊断而非通用文案；③ **关停排空
  静默化 + 关停闸门**——`drain_hook_tasks` 原为单次快照等待，漏掉等待期内派生的
  落盘子任务（审计仍可丢账），现改 deadline 内循环排空到静默；`kernel` 新增
  `_closing` 闸门，`shutdown` 先置位再排空，`handle_message` 对在途关停返回
  `kernel_shutting_down` 系统通知（fail-visible），排空期间不再有新 turn 竞争；
  ④ **关停超时配置化**——`_INFLIGHT_TURN_DRAIN_TIMEOUT_S` 等三个硬编码常量
  挪进 `nanobee.yaml`（`shutdown.drain_inflight_s / drain_cancelled_s / drain_hooks_s`，
  默认值即原常量），并修正与实现不符的注释；⑤ audit `nanobee.error` 落盘前
  **先脱敏再截断**（`_redact_error`：URL `?key=`、`Authorization: Bearer` 等凭证值
  → `<redacted>`，键名保留），第三方异常文案中的密钥不再进入审计文件与
  `[audit-json]` 日志；⑥ `on_message_started` 改「声明才调度」（payload 携带
  用户原始输入，声明即能力，数据最小化）；completed 维持全量调度并修正注释。
  新增 `tests/test_context_security.py` 归一化/落点断言用例、
  `test_runner_ledger.py` 外层 context_root 与静默排空用例、
  `test_kernel_shutdown_drain.py` 关停闸门用例、`test_audit_logger.py` 脱敏用例、
  `test_hook_scheduling.py` 声明过滤用例。

- **turn 终态保证（Phase 2）**：使「每 turn 恰好一份终态 report」成为框架不变量。
  ① `ExitReason` 新增 `ABANDONED`——turn 未产出 runner 结果（状态机异常 / 被
  取消 / 关停排空超时）时，`_process_message` 的 finally 经幂等出口
  `_emit_turn_report` 兜底补发 `exit_reason="abandoned"` 的 report（账本仅含
  窗口锚点，`error` 恒非 None，消息窗口仅含输入侧），审计不再有"凭空消失"的
  turn；② 取消安全：兜底在取消路径内用 `create_task` 登记而非 await，
  `CancelledError` 原样传播，`turn_report_emitted` 标志防止双发；③
  `AgentLoop.drain_hook_tasks(timeout_s)`：fire-and-forget Hook 任务统一登记
  （`_track_hook_task`，完成自清），`kernel.shutdown` 在 turn 排空后有界等待
  审计落盘收口，插件任务不再随关停被取消导致 turn span 丢失；④
  `kernel.shutdown` 对在途 turn 有界排空（`_INFLIGHT_TURN_DRAIN_TIMEOUT_S=10s`，
  机制上界）——接近完成的 turn 优先跑完并正常回复，超时 turn 取消落
  ABANDONED；⑤ audit_logger 的 `on_message_completed` 增加 turn_id 所有权
  校验：单槽状态属于更新一轮时（started 覆盖竞态窗口）不 pop 不覆盖，用独立
  兜底状态落盘，防跨 turn 归并。

### Fixed

- **tool_cron 间隔红线判据修正**：cron 调度的安全红线此前以「首触发距现在 ≥ 30 秒」
  （插入时刻属性）为判据，与「防止任务刷屏」真正需要的「相邻两次触发间隔 ≥ 30 秒」
  （调度表达式属性）错配，造成双向缺陷：① **误伤**——周期完全合法的 `*/2 * * * *`
  在每 2 分钟周期的最后 30 秒被拒（相位窗口 30s/120s，即 25% 的墙钟时间必被拒），
  表现为测试 `test_tool_cron_redline.py::test_cron_two_minutes_accepted` 随运行时刻
  间歇性失败（standalone 复跑通过）；② **漏防**——`* * * * * 0,45`（每分第 0/45 秒
  各触发一次，最小间隔 15 秒，真会刷屏）因"此刻首触发还有 45 秒"在约 75% 的相位
  被放行。现改为按「最小相邻触发间隔」判定：自当前时刻连续探测 60 次触发取最小间隔
  （分钟级表达式间隔恒 ≥ 60 秒必然合规，只有秒级表达式可能出现亚分钟间隔），
  判据与墙钟相位彻底解耦——合法周期在任何时刻都被接受，亚 30 秒间隔在任何时刻都被拒绝。
  `every` / `at` 分支行为不变（两者判据本就正确，`every` 的首触发即周期、`at` 只有
  首触发一个含义）。有意不保留"首触发下界"检查：它不构成"间隔"、对防刷屏无贡献，
  保留会重新引入"同一句指令时而成功时而失败"的非确定性；若日后需抑制"创建即触发"，
  应改为把 `next_run_at_ms` 顺延而非拒绝。测试改为多相位扫描 + 冻结时钟，
  两个方向的缺陷各有专属回归用例（10 → 13 用例）。


- **安全**：`ContextManager` 对 `user_id` 增加白名单拒绝式校验（仅允许
  `[A-Za-z0-9._-]`，长度 1-64，不得为 `.` / `..`，违规 raise `ContextError`）。
  此前 `user_id` 未净化直接用于拼接磁盘目录与审计文件名
  （`users/<user_id>/`、`audit_logger/<user_id>.jsonl`），HTTP 通道的
  `conversation_id` 由请求体控制（`api_key` 未配置时无鉴权），可构造
  路径遍历向预期目录外写入任意 `*.jsonl`。现 `ContextManager.get_or_create`
  是全框架唯一的 user_id 守卫点（`switch` 经其委托同样收口）；拒绝而非净化，
  避免别名碰撞导致跨租户数据串写。**部署注意**：历史遗留的非法 user 目录
  不做自动迁移，若有需人工改名。
- **正确性**：turn 异常折叠路径的消息窗口不再退化为全量历史。
  此前 `run()` 的 except 分支新建的账本未设 `turn_input_index`（默认 0），
  loop 侧 `messages[0:]` 会把 system + 全量历史记为本轮窗口——审计归因
  错位在失败 turn 复活。现账本在 `run()` 入口创建并以参数贯穿
  `_run_core` 与异常折叠路径（单一账本，结构上杜绝第二个账本）。
- **正确性**：触达迭代上限后的排空注入补记账。第 7 处排空点
  （`after max_iterations`）此前未传账本，注入消息写入了历史且
  `had_injections=True`，但没有 `InjectionFact`——账本与 `had_injections`
  自相矛盾、注入次数少记。
- **audit_logger 健壮性**：① `nanobee.error` 落盘前截断（新配置
  `error_max_chars`，默认 500）并折叠空白——异常串常含 URL / 内部路径 /
  上游响应体，直接落 JSONL 与结构化日志扩大泄敏面，且 U+2028 等行分隔符
  会破坏 JSONL 行判别；② 落盘序列化从两份合为一份（文件写与
  `[audit-json]` 日志共用同一字符串）；③ 回退 tool span 独立落行（同数据
  此前落 2~4 份、每 turn 写盘 1→N+1 次），恢复「一行 = 一个 turn」；④
  `_completed` 测试辅助列表改有界 deque（64），长驻实例内存不再无界增长；
  ⑤ `tool_calls` 计数单一来源（completed 的 `len(tool_spans)`），删除
  pre_invoke 的冗余累加。

- **钉钉通道**：richText 入站消息不再被静默丢弃。
  钉钉 Stream 回调的 richText 项是裸 `{"text": ...}`（不带 `type` 字段），
  原判据 `item.get("type") == "text"` 导致所有文本项被跳过 → 内容为空 →
  消息被判空丢弃，agent 完全不触发（单聊富文本与群聊 @ 均命中，用户侧表现
  为"机器人没有任何回复"）。现按「全面对齐 SDK 判据」修复：文本项
  `"text" in item`、媒体项 `"downloadCode" in item`（与
  `dingtalk_stream` SDK 的 `get_text_list` / `get_image_list` 一致），
  两条判据彼此独立（同项同时含文本与下载码时两者都保留），保留
  `fileName`，并对 `text=None` 容错。
  已通过真实钉钉环境端到端验证（dws 群 @ 富文本注入 + dws 单聊富文本注入）。

- **MCP**：连接生命周期重构为「每 server 一个 owner task」，消除跨 task 关闭
  导致的「MCP 清理错误（可忽略）」。
  anyio 的 cancel scope 归属「进入它的那个 task」，跨 task 退出会抛
  `RuntimeError: Attempted to exit cancel scope in a different task`；且同一
  task 内并发持有的多个 scope 严格嵌套、只能整体逆序退出，无法只关中间某一个。
  原实现用单个全局 AsyncExitStack：连接在 boot / 消息 task 进入，关闭在信号
  守卫 task 执行，于是关闭被静默吞掉、先进入的 server 永远没被拆解（连接与
  子进程泄漏）。现每个 server 独占一个 owner task，建立 / 关闭 / 重连都在该
  task 内完成，调用方只投递指令并等待结果。
  实机验收：该错误 09-11 全天 12 次 → 09-12 全天 0 次。

- **MCP**：连接层日志不再输出第三方异常原文，消除 URL query 中网关 key 的
  泄漏面。
  httpx / anyio 的异常消息内嵌完整请求 URL（实测 httpx 0.28.1
  `HTTPStatusError` 的 str 含 `for url '...?key=...'`），而 loguru 的
  `logger.exception` 会把 traceback 并入日志正文。现连接失败分支只记异常类名
  与经 `_redact_url` 脱敏的 URL 主干。脱敏责任归连接层——它是第一个同时掌握
  敏感输入与异常对象的 nanobee 自有边界，往下的第三方文案不可控。

- **Kernel**：命令拦截先于 MCP 连接。
  原顺序下零 token 路径（`/stop`、`/help`）会被卡死的 MCP server 拖住最长
  `CONNECT_ATTEMPT_TIMEOUT_S`（30s）；现命令先被拦截，不付出连接等待。

- **Kernel**：`close_mcp()` 移到通道任务收口之后。
  关闭之后仍在飞行的连接请求会新建一条无人回收的 owner（连接 / 子进程 /
  task 三重泄漏）。

### Changed

- **钉钉通道**：流式回复日志降噪，遵循「高频明细进 TRACE，DEBUG 只留状态
  与异常」。
  `stream_content` 逐 chunk 明细（完整内容与成功响应体）由 DEBUG 降为
  `TRACE`；`finish_streaming` 的 body（含完整 msgContent，与回复正文重复）
  同步降级。DEBUG 仅保留一次性状态行与**非 200 响应**（QpsLimit/500 等故障
  仍默认可见）。需完整回放时将实例配置 `logging.level` 设为 `TRACE`
  （loguru 原生级别，文件 sink 直接透传，无需改动 schema）。
  实测（DEBUG 级，5 个 chunk + 终态 + 1 个 500 异常）：相关日志 12 行 → 2 行。

- **MCP**：`MCPManager` 重写 —— 连接状态由每 server 的 owner task 持有；
  `connect()` 幂等且稳态零开销零日志（kernel 每条消息都会调用，故连接中的
  server 会让后来者等待同一结果，不再出现「首轮缺失 MCP 工具」的竞态）；
  单 server 失败不影响其它 server；`close()` 并行 gather + fail-closed
  （仅摘除已退出的 owner，期间到来的 `connect()` 复用正在关闭的 owner，
  不产生逃逸回收的连接）。

- **MCP**：新增机制层超时常量并公开化，作为跨模块预算推导的单一来源 ——
  `CONNECT_ATTEMPT_TIMEOUT_S` / `PER_SERVER_OP_TIMEOUT_S` /
  `OP_WAIT_SLACK_S` / `RECONNECT_WAIT_TIMEOUT_S` / `CLOSE_WAIT_TIMEOUT_S` /
  `RETRY_COOLDOWN_S`。外部等待预算恒大于内部执行上界之和，避免慢启动 server
  （npx 冷启动）假超时。`stack.aclose()` 用 `asyncio.timeout` 上界而非
  `wait_for`——后者会把协程挪到新 task，cancel scope 将在错误的 task 退出。

- **MCP**：连接失败进入冷却期（`RETRY_COOLDOWN_S`，构造参数
  `retry_cooldown_s` 可注入），避免挂死型故障（stdio 起不来）下每条消息
  重付一次完整建连。

- **ChannelManager**：MCP 连接任务由 `ensure_future` 发射即忘改为
  `create_task` 持引用 + `done_callback` 异常取回 + 关停前有界等待
  （`wait_background`）。原实现下连接失败彻底静默（「Task exception was
  never retrieved」）。关停是「等它落地」而非「取消」——取消只中断 `connect()`
  内部的 await，各 server 独立的 owner task 仍会继续建连，收不到口。

- **MCP**：`connect_mcp_servers` 一次只允许一个 server（多 server 编排归
  `MCPManager`），入口有运行时断言；`unregister_server_tools` 与
  `attach_reconnect_handlers` 提升为公开 API，匹配按 wrapper 归属
  （`server_name` 精确匹配）而非名称前缀（避免 server 名 `a` 与 `a_b`
  互为前缀时误伤对方）。

- **AgentLoop**：`_connect_mcp` 改为公开 `connect_mcp()`，kernel 不再访问
  私有方法。

### Added

- `tests/test_channel_dingtalk.py::TestRichTextParsing`：richText 解析
  回归测试 9 条（裸 text 项、`process()` 不再丢弃、群 @ 报文、媒体项与
  `fileName` 保留、文本+下载码同项双保留、显式 `type` 形态兼容、
  `text=None` 容错、空 richText 列表、`msgtype=text` 路径不受影响）。
- `tests/test_card_stream_log_level.py::TestStreamRespLogLevel`：流式
  响应日志分级契约测试 2 条（200 响应不落 DEBUG、非 200 响应必落 DEBUG
  且携带 status 与 body）。

- `tests/test_mcp_lifecycle.py`：MCP 连接生命周期行为测试。替身使用**真实
  anyio task group** 而非 AsyncMock——后者会掩盖 cancel scope 的宿主 task
  问题（被替换掉的旧 `test_mcp_manager.py` 即如此）。覆盖：幂等、default_cwd
  透传、单 server 失败不影响他人、稳态零日志、冷却期、并发 connect 等待同一
  连接、拆解每一个 server、close 幂等、关闭中 connect 不泄漏、关闭超时保留
  登记、拆解组异常就地收住、close 并行、重连重建、owner 复活后可再次关闭。

- `tests/test_mcp_lifecycle_probe.py`：anyio 约束探针 4 条，固化本重构的
  设计前提——跨 task 关闭必抛、单 task 内多 scope 先关先进入者必抛、逆序退出
  无异常、无法只关中间 scope。若哪天这些用例失败，说明 anyio 语义变更，
  MCP 生命周期设计需重新评审。

- `tests/test_mcp_tools.py`：工具注册 / 注销的所有权语义测试（按 `server_name`
  归属而非前缀）、`connect_mcp_servers` 单 server 断言，以及连接失败日志
  不泄漏 URL key 的回归测试。

### Removed

- `tests/test_mcp_manager.py`：随 MCP 生命周期重构删除。其 AsyncMock 替身
  无法复现 cancel scope 的宿主 task 语义，正是旧实现问题长期隐藏的原因；
  相关覆盖已由 `test_mcp_lifecycle.py` 接管。

### Changed

- **出站契约收敛为单一入口 `nanobee.outbound`**：出站模型此前有三条 import 路径
  （`nanobee.outbound`、`nanobee.agent.messages` re-export、`nanobee.channel.message`
  re-export），同一模型的字段与语义会在多个命名空间下各自演进，是「第二真相源」的温床。
  现出站模型只保留 `nanobee.outbound` 一条入口，`agent.messages` 只承载 `InboundMessage`；
  内核、通道基类、cron 与各通道插件的 import 全部改指唯一入口。
  通道基类的出站分发改经 `outbound.publish_outbound`（事件型出站共三个发布者：
  cron 结果、kernel 注入、子代理通知），正常回复仍走 `handle_message` 返回值直投。

- **通道基类新增 `supports_push` 能力声明，出站守卫放宽到「正文与附件至少一个非空」**：
  `supports_push: bool = True` 声明该通道能否被主动推送；pull 模型通道（HTTP 的 `send()`
  为空实现，出站由调用方自行拉取）应置 `False`，发布侧据此如实报告「投递失败」，
  而不是静默丢弃却判成功。基类 `_on_agent_outbound` 按契约整体透传 `media`，
  守卫由「正文非空」放宽为「正文与附件至少一个非空」——纯附件（正文为空）是合法形态，
  是否投递附件由各通道自行决定（钉钉走卡片 + 附件、CLI 已知取舍忽略并记 debug、HTTP 为 pull 模型）。
  CLI 通道随入站旁路收口改为**直连内核**（用户输入 → `kernel.handle_message` → `send()`），
  投递失败不再有旁路兜底。

- **`message` 工具契约收窄为「只投递附件」**：删除 `content` 参数，`media` 必填且
  `minItems: 1`（声明层拒绝，非法调用到不了执行体）。此前正文既可走本工具、又可走最终回复，
  同一条信息两处都可承载，模型只能在两者间反复猜测；且原回执承诺了工具无法感知的投递结果。
  现契约明确「要送达的正文必须写在最终回复里」，回执只陈述已经发生的事实
  （登记了哪几个附件），不承诺尚未发生的投递；参数形状由 `ToolRegistry.prepare_call`
  前置校验，工具内不再重复守卫。

- **会话工具轨迹落盘（`agents.defaults.persist_tool_traces`，默认 `false`）**：
  会话历史此前只落「user 原文 + assistant 终文本」，工具调用链全部丢失——回看历史时
  只见「宣称完成」的终文本，看不到「先调工具才宣称完成」的因果链，失败轮与中断轮更是
  只剩宣称、无任何执行痕迹。现按增量把本轮 `assistant(tool_calls)` 声明与 `tool(result)`
  结果一并落盘：
  ① **落盘顺序**：轨迹先落、终文本后落，失败/中断轮同样留痕；
  ② **增量切片** `_turn_increment(ctx)` 尊重 runner 内部真实顺序（`max_iterations` 出口会先
  追加终文本、再追加注入的 user 消息），轮内注入（drain）的 user 条目如实入账——
  此前这类条目从未落盘；
  ③ **配对校验 + 悬尾修复**：以已落盘历史为基准做声明/结果配对（兼容崩溃恢复后补落的孤儿结果
  与跨 turn 重复落盘），对「声明已落盘、而增量与历史都无结果」的调用合成
  `[tool call cancelled: turn interrupted before result]` 占位结果——工具被守卫拦截 / turn 中断 /
  崩溃恢复三种成因下的历史断档就此消灭（悬尾判定必须看整段增量，逐条处理会把「还没轮到的结果」
  误判为缺失而多落一条）；
  ④ **三层清洗**：call id 归一为 `str`（避免 int id 半截匹配）、剔除未声明/重复的工具结果
  （会直接导致后续 provider 请求协议报错）、部分 provider 对 tool 消息 `name` 非空的隐含要求
  缺失即省略该键；
  ⑤ **出生点脱敏先于截断**：顺序颠倒会被截断切断密钥形态而漏出半截凭证；触顶落
  `(persist truncated)` 标记，与面向模型的 `truncate_text` 后缀区分，回查时能分辨
  「落盘时被收紧」与「模型侧被截断」；
  ⑥ **上界全部配置化**：`tool_result_persist_max_chars` / `tool_args_persist_max_chars`
  （默认 8192，与面向模型的 `max_tool_result_chars` 解耦——持久语义更紧），思维链默认剥离，
  联调需要时 `persist_reasoning: true`；
  ⑦ **开关语义**：关闭时严格回退旧口径（只落 user 原文 + assistant 终文本），
  读取侧自愈逻辑不受开关影响（「开关只控写入」）；
  ⑧ **单一写入路径**：会话侧新增 `Session.add_protocol_message()`，非法 role / 缺协议键
  当场 `raise`，不静默写坏历史；浅拷贝隔离调用方后续改动，协议键经 JSONL 序列化/加载往返保持原样。

- **上下文裁剪的协议合法性修复（`_snip_history` + 回放窗口）**：预算裁剪可能停在
  「声明在窗口内、结果被裁掉」的调用组上，而下游 `_backfill_missing_tool_results`
  会为该调用补合成结果——等于把刚被裁掉的内容又请回窗口，且发生在预算判定之后。
  新增 `utils.helpers.find_legal_message_end`（与既有 `find_legal_message_start` 成对），
  裁剪后先对齐首条 user，再丢掉头部孤儿工具结果与尾部未完成的调用组。两个接入点：
  runner 预算裁剪、BUILD 安全阀 `AgentLoop._repair_replay_window_head`（覆盖 memory skill
  裁剪后的历史）。「已经没有任何合法窗口」的末路兜底**刻意不做尾部修复**——再裁会退化成
  只剩 system、模型完全失去上下文，协议合法性交给下游兜底。纯文本历史（未开启轨迹落盘）
  两侧均为 no-op。

- **钉钉媒体读取安全策略（Phase 1 安全前置）**：媒体读取此前缺少白名单与体积约束，
  SSRF 判定在钉钉侧另有一份字符串黑名单实现，与 `nanobee.security.network` 存在规则漂移。
  现：① SSRF 判定收敛到仓内唯一实现 `nanobee.security.network`，钉钉侧删除自持黑名单；
  ② 绕过写法（十进制/八进制/十六进制 IP、链接本地元数据段、IPv6 私网与映射）全部拒绝，
  重定向逐跳复检；③ 本地附件读取受白名单根约束（`media_local_roots`，相对路径按 `data_dir`
  解析；`data_dir` 与入站附件目录 `./media/dingtalk` 始终放行），`..` 穿越、符号链接逃逸、
  `file://` 越界均拒绝；④ 体积上限 `media_max_mb`（远端与本地共用，`ge=1`）与总开关
  `enable_media_upload` 生效，分块读取（1MB/块）避免大文件全量入内存，路径解析与判定走线程
  不阻塞事件循环，预检与读取之间文件被替换/增长时仍拒绝（不返回被截断内容）；
  ⑤ 策略注入点 `DingTalkSender.set_media_policy`（由通道 `start()` 调用）；
  ⑥ 内核启动接线 `tools.ssrf_whitelist` → `configure_ssrf_whitelist`（传空列表即复位，
  多实例/测试交替构造不残留），保证内网 CIDR 逃生通道在任何媒体读取前生效；
  ⑦ 配置注释与实现对齐：显式标注 `enable_chunk_upload` / `chunk_size_kb` **尚未接线**
  （分片上传本身已实现，阈值/块大小仍是代码内常量），避免「配置了却不生效」。

- **cron 事件型出站如实报告投递结果**：目标通道显式声明 `supports_push=False`（pull 模型）时
  直接判投递失败并记 warning，由调用方如实上报，不再「静默丢弃却报成功」；
  同时放开纯附件投递（周报形态 `content=""` + 单个 MD 附件）。语义变化严格限定在
  「已知且明确声明不可推送」这一种情形：通道未知/未加载、无 `plugin_manager`、
  无有效投递目标一律维持现状（视为可推送），避免扩大爆炸半径。

### Added

- `nanobee/session/session_audit.py`：会话文件协议契约校验与度量（只读审计 CLI，
  `python -m nanobee.session.session_audit <文件或目录>`）。协议消息一旦落错
  （孤儿结果、悬尾声明、重复结果），发给 provider 会直接协议报错，而错误现场在会话文件里、
  不在日志里——本模块提供「把会话文件当契约来查」的能力，覆盖六类违规（V1..V6）。
  职责边界：只做「读文件 / 判契约 / 算度量」，不修复（修复属回放侧自愈）、不判定放行、
  不写文件、不打印原始内容。两个落盘标记常量与 `loop` 侧由交叉一致性断言锁死，任一侧漂移即红。

- 配置项：`agents.defaults.persist_tool_traces` / `persist_reasoning` /
  `tool_result_persist_max_chars` / `tool_args_persist_max_chars`；
  钉钉 `enable_media_upload` / `media_max_mb` / `media_local_roots`。

- 测试 8 个新文件共 190 用例：`test_tool_trace_persistence.py`（31）、
  `test_tool_trace_volume_baseline.py`（13，只锁机制硬不变量，经验数值留给评测集基线报告）、
  `test_replay_window_legality.py`（19）、`test_session_protocol_messages.py`（15）、
  `test_session_audit.py`（43）、`test_dingtalk_media_security.py`（53）、
  `test_supports_push.py`（11）、`test_channel_cli_plugin.py`（5）。

### Removed

- **`tool_web` 插件下线**（web_search / web_fetch）。该插件自 2026-06-07 起即在
  `plugin.toml` 中 `enabled = false`（实例日志可印证「已配置为禁用状态，跳过启用」），
  搜索质量不满足使用要求，故整体移除而非继续维护。同步清理：README 内置插件表行、
  `docs/plugin_development.md` 的 `requires` 依赖示例（改用 `tool_fs`）、
  `skill-creator` 优雅降级示例中对 `web_fetch` / `readability-lxml` 的引用
  （否则技能会指示模型调用已不存在的工具），以及唯一消费者依赖组 `nanobee[web]`
  （`duckduckgo-search` / `readability-lxml` / `lxml`）与 `dev` 组里的 `nanobee[web]`。

- `nanobee/utils/searchusage.py`：web 搜索提供商的用量查询（原供 `/status` 使用），
  全仓零 import、零测试、未在 `utils/__init__.py` 导出，属搜索能力的同源残留，一并删除。

- `nanobee/channel/message.py`（整文件）与 `nanobee/agent/messages.py` 的出站 re-export：
  出站契约收敛到 `nanobee.outbound` 后的删除项（见 Changed）。

- 通道基类死接口：`handle_incoming` / `_process_incoming` 入站旁路与 `ChannelMessage`
  （该链路从不承载生产流量，唯一调用点在永不启动的 CLI 交互循环内）、
  `send_delta` / `send_reasoning_delta` / `send_reasoning_end` / `StreamingDelta`
  （全仓零调用零构造，流式实际走 `on_stream` / `on_stream_end` 回调）、
  `supports_streaming` / `_stream_supported`（write-only，写入后无人读取）、
  `pairing_code` / `is_allowed`（生产零赋值，恒为 no-op）。
