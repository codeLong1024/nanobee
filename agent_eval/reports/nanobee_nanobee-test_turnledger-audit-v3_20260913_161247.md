# TurnLedger / audit v3 agent 级回归报告

- 实例：nanobee-test（开发机直跑工作区源码 editable）
- 时间：2026-09-13T16:13:16
- 结论：✅ ALL PASS
- 用例：cases/turn-ledger_audit-v3_regression_cases.md

| 用例 | 判定 | duration_ms | iterations | tool_calls | finish_reasons | total_tokens | trace_id |
|---|---|---|---|---|---|---|---|
| A1 纯聊天零工具 turn | PASS | 3184.425 | 1 | 0 | ['stop'] | 80216 | 05e64a696dcc9ebec4fe7a0e88b05a25 |
| A2 写后读回多工具链 | PASS | 10970.497 | 3 | 2 | ['tool_calls', 'stop'] | 241213 | e22635bfddafd1d2d79c6d8004ce16eb |
| A3 工具越界错误捕获 | PASS | 9447.295 | 2 | 0 | ['tool_calls', 'stop'] | 160759 | 5ca09d3c88c78a3c295ea9fba84db5ac |

## 失败明细

- INFO **A3**：LLM 主动拒答未调工具（tool_calls=0），INFO 不判 FAIL

## 残留物

- `users/user/sessions/direct_av3-*.jsonl`（会话历史，测试实例预期）
- `users/user/audit_logger/user.jsonl` 追加了 av3 turn 行（审计真值，保留）
- A2 可能在工作目录留有 `av3_probe.txt`（测试用户工作区，无害）
- 临时 config `/tmp/nc-nanobee-test-iter.yaml` 已删（含密钥不留盘）
