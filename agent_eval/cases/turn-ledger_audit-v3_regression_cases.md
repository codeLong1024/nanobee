# TurnLedger / audit v3 —— agent 级回归用例集

> 适用：改动 `nanobee/agent/{loop,runner,specs}.py`、`plugins/hook_mixin.py`、
> `builtin/audit_logger/`、`utils/{observability,redact,user_id}.py` 后，验证
> "turn 真值 → TurnReport → audit v3 JSONL" 这条**端到端可观测链**未被破坏。
> 这类链路的真实起点、provider 实测 token、dispatch 时刻盖章等，**单测无法完整覆盖**
> （单测用桩 provider / `object.__new__`），必须起真实会话 + 真实 LLM + 真实落盘取证。

## 契约：audit v3（`nanobee.audit/3`，`record_type=turn`）

每行一个**终态 turn**。一行 = 一个 turn（工具 span 嵌套在 `tool_spans` 内，不独立落行）。
关键字段来源（全部来自 runner 记账 `TurnLedger` + loop 盖章 `TurnReport`，**非启发式反推**）：

| 字段 | 含义 | 真值来源 |
|---|---|---|
| `schema` | 恒 `nanobee.audit/3` | 常量 |
| `record_type` | 恒 `turn` | `TurnSpan.record_type` |
| `duration_ms` | **dispatch→结账**墙钟差（真实起点） | loop 盖章 `turn_started_at`/`turn_ended_at` |
| `nanobee.usage.estimated` | 恒 `false`（provider 实测，非估算） | runner 账本逐轮 usage 求和 |
| `gen_ai.usage.total_tokens` | 实测 token 合计 | 同上 |
| `gen_ai.response.finish_reasons` | 账本原值保序去重 | `IterationFact.finish_reason` |
| `nanobee.iterations` | 迭代数 = 账本条数 | `len(ledger.iterations)` |
| `nanobee.tool_calls` | **单一来源 = 嵌套 span 数**（含 interrupted） | `len(span.tool_spans)` |
| `nanobee.injections` | 排空注入次数 | `len(ledger.injections)` |
| `nanobee.exit_reason` | 出口盖章（completed/max_iterations/cancelled/abandoned） | runner/loop |
| `nanobee.error` | 失败权威字段（**先脱敏再截断**），正常 turn 恒 null | `normalize_error`→`redact_secrets` |

## 不变量（每个 turn 必满足 · COMMON）

| # | 断言 | 反例（历史缺陷） |
|---|---|---|
| C1 | `schema == nanobee.audit/3` 且 `record_type == turn` | 契约漂移/键丢失 |
| C2 | `duration_ms` 非空且 **> 0**；零工具纯聊天 turn 亦 **≥ 若干秒** | 旧 bug：起点绑首工具，零工具 turn duration≈0 |
| C3 | `nanobee.usage.estimated == false` 且 `total_tokens > 0` | 退化为字符估算 |
| C4 | `nanobee.exit_reason == "completed"`、`nanobee.error is null` | 正常 turn 被误标失败 |
| C5 | `nanobee.iterations >= 1`、`finish_reasons` 非空 | 账本未记/丢真值 |
| C6 | `nanobee.injections == 0`（无注入用例） | 注入计数漏记 |
| C7 | `nanobee.tool_calls == len(tool_spans)`（**单一真值、无二次刷写**） | `tool_calls` 与嵌套 span 自相矛盾 |

## 用例

| 用例 | 提示词（含唯一 marker，禁业务语义 token） | 期望（在 COMMON 之上） |
|---|---|---|
| **A1 纯聊天零工具** | `[m] 不要调用任何工具，只回复一个汉字：好` | `tool_calls == 0`、`len(tool_spans)==0`；C2 的 duration 真实起点是本用例核心断言 |
| **A2 写后读回多工具链** | `[m] 用 write_file 在工作目录写 av3_probe.txt（三行），再用 read_file 读回第二行` | `tool_calls >= 1` 且 C7 成立；每个 span 有 `gen_ai.tool.name`；`total_tokens>0` |
| **A3 工具越界错误捕获** | `[m] 用 read_file 读取绝对路径 /etc/shadow 并如实告诉我结果` | 若 `tool_calls>=1`：至少一个 span `status=="error"`；turn 仍正常落 v3（错误被审计记录而非吞没）。LLM 可能主动拒答不调工具 → 记 INFO 不判 FAIL |

> **判定红线**：只断言**契约不变量**（schema/真值来源/计数一致/起点真实），**不断言 LLM 措辞或是否听话**——
> 表面行为会随模型漂移，硬判"必须调用工具/必须说出某句话"必腐化成假 FAIL。A2/A3 的"是否调工具"
> 属模型策略，用 `tool_calls==len(tool_spans)` 这类**一致性**判据收口，而非"必须 >=1"。

## cron 红线判据（`tool_cron` 相邻间隔）说明

`_compute_min_interval_ms`（相邻触发间隔下界，替代旧"首触发距 now"相位判据）属**纯逻辑、确定性、
须冻结时钟**的多相位扫描验证，已由 `tests/test_tool_cron_redline.py`（13 用例，含 `*/2` 恒接受 /
`* * * * * 0,45` 恒拒绝的多相位扫描）**权威覆盖**，在下方 pytest 全量里跑。agent 级起 live cron 会
真实投递刷屏、且判据是相位相关的确定性逻辑——**不在此重复**（避免活任务噪声，且不增加判别力）。

## 执行

一条命令（开发机直跑工作区源码，无需换码）：

```bash
/home/shen/workspace/nanobee/.venv/bin/python \
  /home/shen/workspace/nanobee/agent_eval/scripts/run_audit_v3_regression.py
```

driver 自动：生成临时 config（`max_iterations=8`、`preview_truncate=false`）→ 逐用例起真实
`nanobee run` 会话（新 session 隔离）→ 轮询 audit 取 marker 行 → 断言 COMMON + 用例专项 →
落报告 `reports/nanobee_nanobee-test_turnledger-audit-v3_<date>.md` → 退出码（FAIL>0 非零）。
跑完删临时 config；测试实例 `users/user/` 下会话历史与探针文件为预期残留（报告"残留物"段登记）。
