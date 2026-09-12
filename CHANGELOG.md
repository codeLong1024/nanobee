# 变更日志（Changelog）

本项目所有显著变更记录于此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

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
