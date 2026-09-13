# agent_eval —— nanobee agent 级回归评测集（开发机）

> 定位：沉淀**可复跑**的 agent 级回归用例（真实会话 + audit 取证判定），区别于 `tests/`
> 下的函数/单测。本目录在开发机随工作区维护；`nanobee-qa` 技能「回归测试闸门」的 agent 级
> 冒烟在此成集。判定红线见各用例文档（只断言契约不变量，不断言 LLM 措辞/是否听话）。

## 目录结构

| 目录 | 内容 | 命名 |
|---|---|---|
| `cases/` | 用例文档（契约字段 + 不变量表 + 用例矩阵 + 执行方式） | `<主题>_regression_cases.md` |
| `scripts/` | driver / checker（一条命令跑用例 + 解析 audit + 判定） | `run_<主题>_regression.py` |
| `reports/` | 跑出的判定报告（含汇总表 + 失败明细 + 残留物） | `nanobee_<实例>_<主题>_<日期>_<时分秒>.md` |

## 用例集清单

| 主题 | 被测面 | 用例文档 | driver | 最近结果 |
|---|---|---|---|---|
| TurnLedger / audit v3 | `agent/{loop,runner,specs}` + `plugins/hook_mixin` + `audit_logger` + `utils/{observability,redact,user_id}` 的 turn 真值端到端链 | `cases/turn-ledger_audit-v3_regression_cases.md` | `scripts/run_audit_v3_regression.py` | ✅ ALL PASS（2026-09-13 161247：A1 真实起点3.1s / A2 多工具链 tool_calls==len(spans)==2 / A3 越界读 LLM 拒答 INFO） |

## 覆盖口径

- **agent 级**（本目录）：端到端真实会话 + audit v3 契约不变量（真实起点/实测 token/账本原值 finish_reasons/单一真值计数/脱敏）。单测桩无法覆盖 provider 实测与 dispatch 时刻盖章。
- **单测层**（`tests/`，非本目录）：cron 红线相邻间隔判据（`test_tool_cron_redline.py` 多相位扫描）、`normalize_error`/`redact_secrets`（`test_redact.py`）、`resolve_storage_key`（`test_context_security.py`）等确定性逻辑在此权威覆盖，回归时随 pytest 全量跑。

## 跑法

```bash
cd /home/shen/workspace/nanobee && source .venv/bin/activate
pytest tests/ -q                                              # 单测基线（当前 1344 passed / 1 skipped）
python agent_eval/scripts/run_audit_v3_regression.py         # agent 级 audit v3 回归
```

driver 自动：生成临时 config（`max_iterations=8`、`preview_truncate=false`、YAML 合并写回）→
逐用例新 session 起真实 `nanobee run` → 轮询 audit 取 marker 终态 turn → 断言 COMMON + 用例专项 →
落 `reports/` → FAIL 非零退出；跑完删临时 config。开发机 editable，**改码即生效、无需换码/重启常驻**。

## 约定

- 新增用例：用例文档 + driver 同批落地并回填本表"用例集清单"（**改了新文件没进索引 = 未闭环**）。
- marker 用纯随机数字后缀（`zxav3<HHMMSS><id>`），禁 `T1` 这类可能与业务值域碰撞的单字母+数字 token。
