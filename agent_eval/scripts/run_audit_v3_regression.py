#!/usr/bin/env python3
"""TurnLedger / audit v3 —— agent 级回归 driver（开发机直跑工作区源码）。

流程：生成临时 config → 逐用例起真实 `nanobee run` 会话 → 轮询 audit 取 marker 行
→ 断言 COMMON 不变量 + 用例专项 → 落 markdown 报告 → 按 FAIL 数返回退出码。
取证全部走本脚本结构化解析，LLM 不手 grep jsonl。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

BASE = Path("/nanobee-data/nanobee-test/config.yaml")
DATA_DIR = "/nanobee-data/nanobee-test"
AUDIT = Path(DATA_DIR) / "users/user/audit_logger/user.jsonl"
TMP = Path("/tmp/nc-nanobee-test-iter.yaml")
VENV = Path("/home/shen/workspace/nanobee/.venv/bin")
NANOBEE = VENV / "nanobee"
REPO = Path("/home/shen/workspace/nanobee")
REPORTS = REPO / "agent_eval/reports"

# 用例：marker 用纯随机数字后缀，禁与业务值域碰撞（如 "T1"）的单字母+数字形态。
_CASES = [
    {
        "id": "A1",
        "title": "纯聊天零工具 turn",
        "prompt": "[{m}] 不要调用任何工具，只回复一个汉字：好",
        "expect": "zero",
    },
    {
        "id": "A2",
        "title": "写后读回多工具链",
        "prompt": "[{m}] 用 write_file 在当前工作目录写一个文件 av3_probe.txt，"
        "内容三行 alpha/beta/gamma，然后用 read_file 读回它并告诉我第二行",
        "expect": "multi",
    },
    {
        "id": "A3",
        "title": "工具越界错误捕获",
        "prompt": "[{m}] 用 read_file 读取绝对路径 /etc/shadow 并如实告诉我结果",
        "expect": "optional",
    },
]


def gen_tmp_config() -> None:
    """YAML 解析合并写回（禁字符串追加），限迭代 + 关 audit 截断。"""
    cfg = yaml.safe_load(BASE.read_text())
    cfg.setdefault("agents", {}).setdefault("defaults", {})["max_iterations"] = 8
    cfg.setdefault("plugins", {}).setdefault("audit_logger", {})["preview_truncate"] = False
    TMP.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    TMP.chmod(0o640)


def run_session(session: str, prompt: str) -> tuple[int, str]:
    env = dict(os.environ, PATH=f"{VENV}:{os.environ['PATH']}", NANOBEE_DATA_DIR=DATA_DIR)
    proc = subprocess.run(
        [str(NANOBEE), "run", "-c", str(TMP), "-s", session, "-m", prompt],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    return proc.returncode, " | ".join(tail)


def wait_turn(marker: str, timeout_s: int = 30) -> dict | None:
    """轮询 audit，取含 marker 的终态 turn 记录（落盘有数秒延迟）。"""
    end = time.time() + timeout_s
    while time.time() < end:
        if AUDIT.exists():
            for line in AUDIT.read_text().splitlines():
                if marker not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("record_type") != "turn":
                    continue
                blob = json.dumps(rec.get("gen_ai.input.messages", []), ensure_ascii=False)
                if marker in blob:
                    return rec
        time.sleep(1)
    return None


def common_checks(rec: dict) -> list[tuple[str, bool]]:
    dur = rec.get("duration_ms")
    return [
        ("C1 schema==nanobee.audit/3", rec.get("schema") == "nanobee.audit/3"),
        ("C1 record_type==turn", rec.get("record_type") == "turn"),
        ("C2 duration_ms 非空且>0（真实起点）", dur is not None and dur > 0),
        ("C3 usage.estimated==false", rec.get("nanobee.usage.estimated") is False),
        ("C4 exit_reason==completed", rec.get("nanobee.exit_reason") == "completed"),
        ("C4 error is null", rec.get("nanobee.error") is None),
        ("C5 iterations>=1", (rec.get("nanobee.iterations") or 0) >= 1),
        ("C5 finish_reasons 非空", len(rec.get("gen_ai.response.finish_reasons") or []) >= 1),
        ("C6 injections==0", rec.get("nanobee.injections") == 0),
        ("C7 tool_calls==len(tool_spans) 单一真值",
         (rec.get("nanobee.tool_calls") or 0) == len(rec.get("tool_spans") or [])),
    ]


def case_checks(case: dict, rec: dict) -> tuple[list[tuple[str, bool]], list[tuple[str, str]]]:
    """返回 (硬断言, INFO 项)。"""
    hard = common_checks(rec)
    info: list[tuple[str, str]] = []
    tc = rec.get("nanobee.tool_calls") or 0
    spans = rec.get("tool_spans") or []
    if case["expect"] == "zero":
        hard.append(("A1 tool_calls==0（零工具）", tc == 0))
    elif case["expect"] == "multi":
        hard.append(("A2 tool_calls>=1（确有工具执行）", tc >= 1))
        hard.append(("A2 每个 span 有 gen_ai.tool.name", all(s.get("gen_ai.tool.name") for s in spans)))
        hard.append(("A2 total_tokens>0（provider 实测）", (rec.get("gen_ai.usage.total_tokens") or 0) > 0))
    else:  # optional
        if tc >= 1:
            hard.append(("A3 越界读被记为 span error（错误不吞没）",
                         any(s.get("status") == "error" for s in spans)))
        else:
            info.append(("A3", "LLM 主动拒答未调工具（tool_calls=0），INFO 不判 FAIL"))
    return hard, info


def main() -> int:
    if not NANOBEE.exists() or not BASE.exists():
        print(f"[ABORT] 缺 nanobee={NANOBEE} 或 config={BASE}", file=sys.stderr)
        return 2
    gen_tmp_config()
    stamp = datetime.now(UTC).astimezone().strftime("%H%M%S")
    rows: list[dict] = []
    total_fail = 0
    try:
        for case in _CASES:
            marker = f"zxav3{stamp}{case['id'][-1]}"
            session = f"av3-{case['id'].lower()}-{stamp}"
            prompt = case["prompt"].format(m=marker)
            rc, echo = run_session(session, prompt)
            rec = wait_turn(marker)
            if rec is None:
                total_fail += 1
                rows.append({"id": case["id"], "title": case["title"], "marker": marker,
                             "rc": rc, "verdict": "FAIL", "detail": ["audit 未落 marker 行"],
                             "info": [], "echo": echo})
                print(f"[FAIL] {case['id']} audit 缺行 (rc={rc})")
                continue
            hard, info = case_checks(case, rec)
            fails = [name for name, ok in hard if not ok]
            total_fail += len(fails)
            rows.append({"id": case["id"], "title": case["title"], "marker": marker,
                         "rc": rc, "verdict": "FAIL" if fails else "PASS",
                         "detail": fails, "info": info,
                         "echo": echo,
                         "snapshot": {
                             "duration_ms": rec.get("duration_ms"),
                             "iterations": rec.get("nanobee.iterations"),
                             "tool_calls": rec.get("nanobee.tool_calls"),
                             "finish_reasons": rec.get("gen_ai.response.finish_reasons"),
                             "total_tokens": rec.get("gen_ai.usage.total_tokens"),
                             "exit_reason": rec.get("nanobee.exit_reason"),
                             "trace_id": rec.get("trace_id"),
                         }})
            tag = "FAIL" if fails else "PASS"
            print(f"[{tag}] {case['id']} {case['title']} | dur={rec.get('duration_ms')}ms "
                  f"iter={rec.get('nanobee.iterations')} tool_calls={rec.get('nanobee.tool_calls')} "
                  f"fails={fails}")
    finally:
        TMP.unlink(missing_ok=True)

    _write_report(stamp, rows, total_fail)
    print(f"\n=== 汇总：{'ALL PASS' if total_fail == 0 else str(total_fail) + ' FAIL'} ===")
    return 0 if total_fail == 0 else 1


def _write_report(stamp: str, rows: list[dict], total_fail: int) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).astimezone().strftime("%Y%m%d")
    path = REPORTS / f"nanobee_nanobee-test_turnledger-audit-v3_{date}_{stamp}.md"
    lines = [
        "# TurnLedger / audit v3 agent 级回归报告",
        "",
        "- 实例：nanobee-test（开发机直跑工作区源码 editable）",
        f"- 时间：{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- 结论：{'✅ ALL PASS' if total_fail == 0 else '❌ ' + str(total_fail) + ' 项 FAIL'}",
        "- 用例：cases/turn-ledger_audit-v3_regression_cases.md",
        "",
        "| 用例 | 判定 | duration_ms | iterations | tool_calls | finish_reasons | total_tokens | trace_id |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        s = r.get("snapshot", {})
        lines.append(
            f"| {r['id']} {r['title']} | {r['verdict']} | {s.get('duration_ms')} | "
            f"{s.get('iterations')} | {s.get('tool_calls')} | {s.get('finish_reasons')} | "
            f"{s.get('total_tokens')} | {s.get('trace_id')} |"
        )
    lines += ["", "## 失败明细", ""]
    for r in rows:
        if r["verdict"] == "FAIL":
            lines.append(f"- **{r['id']}**：{r['detail']}（rc={r.get('rc')}）")
    for r in rows:
        if r.get("info"):
            for tag, msg in r["info"]:
                lines.append(f"- INFO **{tag}**：{msg}")
    lines += ["", "## 残留物", "",
              "- `users/user/sessions/direct_av3-*.jsonl`（会话历史，测试实例预期）",
              "- `users/user/audit_logger/user.jsonl` 追加了 av3 turn 行（审计真值，保留）",
              "- A2 可能在工作目录留有 `av3_probe.txt`（测试用户工作区，无害）",
              "- 临时 config `/tmp/nc-nanobee-test-iter.yaml` 已删（含密钥不留盘）",
              ""]
    path.write_text("\n".join(lines))
    print(f"[report] {path}")


if __name__ == "__main__":
    sys.exit(main())
