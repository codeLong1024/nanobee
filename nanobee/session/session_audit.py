"""会话文件协议契约校验与度量 — 只读审计工具。

背景：会话工具轨迹落盘（``persist_tool_traces``）让会话文件里出现了
``assistant(tool_calls)`` / ``tool(result)`` 这两类协议消息。协议消息一旦落错
（孤儿结果、悬尾声明、重复结果），发给 provider 会直接协议报错，而错误现场在
会话文件里、不在日志里——本模块提供"把会话文件当契约来查"的能力，供回归闸门、
实例抽验与后续 Step 2 验收复用。

职责边界：只做「读文件 / 判契约 / 算度量」。不做修复（修复属回放侧自愈）、
不判定放行（放行属报告与用户）、不写文件、不打印原始内容。

层级归属：本模块位于 session 包（不依赖 agent 层），因此无法 import
``agent.loop`` 的落盘常量。两个标记常量在此声明，并由
``tests/test_session_audit.py`` 的交叉一致性断言与 loop 侧锁死，任一侧漂移即红
（"交叉断言防漂移"优先于"跨层 import"，避免 session → agent 的反向依赖）。

标记常量说明：
    - :data:`PERSIST_TRUNCATED_SUFFIX`：落盘时被收紧的标记（与面向模型的
      ``truncate_text`` 默认后缀区分，见 ``agent.loop`` 同名常量）。
    - :data:`CANCELLED_TOOL_RESULT_CONTENT`：悬尾声明的合成取消占位文案。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobee.utils.helpers import estimate_prompt_tokens
from nanobee.utils.logger import logger
from nanobee.utils.redact import redact_secrets

# 落盘截断标记（与 agent.loop._PERSIST_TRUNCATED_SUFFIX 同源，测试锁一致性）
PERSIST_TRUNCATED_SUFFIX = "\n(persist truncated)"

# 悬尾占位文案（与 agent.loop._CANCELLED_TOOL_RESULT_CONTENT 同源，测试锁一致性）
CANCELLED_TOOL_RESULT_CONTENT = "[tool call cancelled: turn interrupted before result]"

# 会话文件中合法的消息角色（system/user 为文本行，assistant/tool 承载协议）
_KNOWN_ROLES = frozenset({"system", "user", "assistant", "tool"})

# 脱敏残留预览长度：预览取自脱敏后的文本，凭证值已被掩码
_PREVIEW_CHARS = 60

# 会话文件后缀与需排除的旁路文件（consolidation 归档是摘要格式，不是消息序列）
_SESSION_SUFFIX = ".jsonl"
_CONSOLIDATION_SUFFIX = ".consolidation.jsonl"


@dataclass(frozen=True)
class Violation:
    """一条契约违规。

    Attributes:
        code: 违规码（``V1``..``V6``），含义见模块级 :data:`_VIOLATION_MEANINGS`。
        index: 违规消息下标（恒为消息下标；当前实现不产生"文件级违规"）。
        detail: 说明文本，只含键名、下标、长度，以及**脱敏且限长**的协议标识预览
            （``role`` / ``tool_call_id`` 属排障必需的身份信息，不是内容正文）。
    """

    code: str
    index: int
    detail: str

    def format(self) -> str:
        """渲染为单行文本（用于 CLI 输出）。"""
        return f"{self.code} @{self.index}: {self.detail}"


# 违规码语义表（唯一真相源：CLI 帮助、报告与用例文档均引用此处）
_VIOLATION_MEANINGS = {
    "V1": "role 非法或缺失（仅允许 system/user/assistant/tool）",
    "V2": "assistant 的 tool_calls 形态非法（非空列表 / 每项含非空字符串 id）",
    "V3": "tool 条目缺非空字符串 tool_call_id",
    "V4": "孤儿工具结果（结果出现在其声明之前）",
    "V5": "悬尾声明（assistant 声明的调用在其后没有任何结果或占位）",
    "V6": "重复工具结果（同一 tool_call_id 落盘两次）",
}


@dataclass(frozen=True)
class AuditReport:
    """一次审计的完整结果。

    Attributes:
        path: 被审计文件路径；内存审计时为 ``None``。
        message_count: 消息条数（不含元数据行）。
        violations: 违规元组；为空即契约合法。
        census: 计数普查（``protocol_rows`` / ``truncated`` / ``cancelled`` /
            ``reasoning_left`` / ``unredacted`` / ``unparsable_lines``）。
            **普查量不是违规**：其中 ``unredacted`` 在当前口径下可非零（终文本与
            用户原文不走脱敏，属已登记项 N1），仅作观察。
        bytes_total: 文件总字节数；内存审计时为消息行字节合计（含换行）。
        bytes_by_role: 按角色聚合的消息行字节数。
        token_estimate: 落盘消息列表的 prompt token 估算（tiktoken 未就绪时降级为
            字符估算 ``len//4``；仅空消息列表为 0）。
    """

    path: str | None
    message_count: int
    violations: tuple[Violation, ...]
    census: dict[str, int]
    bytes_total: int
    bytes_by_role: dict[str, int]
    token_estimate: int

    @property
    def ok(self) -> bool:
        """契约是否合法（无任何违规）。"""
        return not self.violations

    def counts_by_code(self) -> dict[str, int]:
        """按违规码聚合计数（供报告与闸门汇总）。"""
        counts: dict[str, int] = {}
        for violation in self.violations:
            counts[violation.code] = counts.get(violation.code, 0) + 1
        return counts


def _raw_tool_calls(message: dict[str, Any]) -> list[Any]:
    """返回可迭代的 tool_calls 列表；非列表（含缺失）一律返回空列表。

    会话文件是不可信输入：``tool_calls`` 可能是任意 JSON 值（标量/对象）。此处
    统一收口为"非列表即空"，使"形态非法"只由 V2 一处报出，而不是让下游
    ``for call in 5`` 抛 TypeError 打断整次审计（那会把"报违规"变成"扫描崩溃"）。
    """
    raw = message.get("tool_calls")
    return raw if isinstance(raw, list) else []


def _iter_text_payloads(message: dict[str, Any]) -> list[tuple[str, str]]:
    """产出 ``(键路径, 文本)`` —— 需参与脱敏残留 / 截断标记探测的文本片段。

    Args:
        message: 单条消息。

    Returns:
        键路径与文本的列表（跳过空值与非法类型）。
    """
    payloads: list[tuple[str, str]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        payloads.append(("content", content))
    for index, call in enumerate(_raw_tool_calls(message)):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            payloads.append((f"tool_calls[{index}].function.arguments", arguments))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        payloads.append(("reasoning_content", reasoning))
    return payloads


def _describe_value(value: Any, *, limit: int = 40) -> str:
    """把不可信值渲染为"类型 + 长度 + 脱敏限长预览"，不回显完整原文。

    仅用于协议标识（``role`` / ``tool_call_id``）：这两个键承载排障必需的身份信息，
    但同属文件派生值，可能被构造成携带凭证的串，故先过 :func:`redact_secrets` 再限长。
    """
    if isinstance(value, str):
        masked = redact_secrets(value)
        preview = masked if len(masked) <= limit else masked[:limit] + "…"
        return f"str(len={len(value)}) {preview!r}"
    return type(value).__name__


def _declared_ids(message: dict[str, Any]) -> list[str]:
    """按出现顺序返回 assistant 声明的 tool call id。

    判据与正向扫描的 ``declared`` 完全一致：**只认非空字符串 id**。非字符串 id
    已由 V2 报出，此处不再把它算作"声明"，否则同一条非法声明会被 V2 与 V5 重复
    计账，掩盖真正的问题。
    """
    ids: list[str] = []
    for call in _raw_tool_calls(message):
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return ids


def _audit_violations(messages: Sequence[Any]) -> list[Violation]:
    """正向 + 逆向两遍扫描，产出全部 V1..V6 违规（不含度量）。"""
    violations: list[Violation] = []
    declared: set[str] = set()
    fulfilled: set[str] = set()

    # 正向：role/tool_calls 形态、孤儿结果、重复结果
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            violations.append(Violation("V1", index, "消息不是 JSON 对象"))
            continue
        role = message.get("role")
        if role not in _KNOWN_ROLES:
            violations.append(Violation("V1", index, f"role: {_describe_value(role)}"))
            continue
        if role == "assistant":
            if "tool_calls" in message:
                raw_calls = message.get("tool_calls")
                if not isinstance(raw_calls, list) or not raw_calls:
                    violations.append(
                        Violation(
                            "V2", index,
                            f"tool_calls 不是非空列表（{type(raw_calls).__name__}）",
                        ),
                    )
                else:
                    for call_index, call in enumerate(raw_calls):
                        if not isinstance(call, dict):
                            violations.append(
                                Violation("V2", index, f"tool_calls[{call_index}] 不是对象"),
                            )
                            continue
                        call_id = call.get("id")
                        if not isinstance(call_id, str) or not call_id:
                            violations.append(
                                Violation(
                                    "V2", index,
                                    f"tool_calls[{call_index}].id 不是非空字符串",
                                ),
                            )
                        else:
                            declared.add(call_id)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                violations.append(
                    Violation("V3", index, "tool_call_id 不是非空字符串"),
                )
                continue
            if call_id not in declared:
                violations.append(
                    Violation("V4", index, f"孤儿结果 {_describe_value(call_id)}"),
                )
            elif call_id in fulfilled:
                violations.append(
                    Violation("V6", index, f"重复结果 {_describe_value(call_id)}"),
                )
            fulfilled.add(call_id)

    # 逆向：悬尾声明（声明之后没有任何结果/占位）
    satisfied: set[str] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                satisfied.add(call_id)
            continue
        if role != "assistant":
            continue
        for call_id in _declared_ids(message):
            if call_id not in satisfied:
                violations.append(
                    Violation("V5", index, f"悬尾声明 {_describe_value(call_id)}"),
                )

    violations.sort(key=lambda item: (item.index, item.code))
    return violations


def _audit_census(messages: Sequence[Any], *, unparsable_lines: int) -> dict[str, int]:
    """计数普查（含脱敏残留探测；均为观察量，不参与契约合法性判定）。

    ``unredacted`` 只能识别"键名 + 值"形态的凭证（如 ``api_key=...``）——裸 token
    （``sk-...``、JWT、高熵串）不带键名时不会被命中，故它给出的是**下界**：为 0
    不代表文本里没有凭证。另外该口径下非零是预期的（终文本与用户原文不走脱敏，
    属已登记项 N1）。
    """
    census = {
        "protocol_rows": 0,
        "truncated": 0,
        "cancelled": 0,
        "reasoning_left": 0,
        "unredacted": 0,
        "unparsable_lines": unparsable_lines,
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "tool" or (role == "assistant" and _raw_tool_calls(message)):
            census["protocol_rows"] += 1
        if message.get("reasoning_content") or message.get("thinking_blocks"):
            census["reasoning_left"] += 1
        if isinstance(message.get("content"), str) and (
            message["content"] == CANCELLED_TOOL_RESULT_CONTENT
        ):
            census["cancelled"] += 1
        payloads = _iter_text_payloads(message)
        if any(text.endswith(PERSIST_TRUNCATED_SUFFIX) for _key, text in payloads):
            census["truncated"] += 1
        if any(redact_secrets(text) != text for _key, text in payloads):
            census["unredacted"] += 1
    return census


def _audit_bytes(messages: Sequence[Any]) -> tuple[int, dict[str, int]]:
    """按角色聚合消息行字节数（与 ``SessionStore.save`` 的写出形态一致）。

    非 dict 与"dict 但 role 非法"分列两个哨兵键（``<non-dict>`` / ``<no-role>``），
    避免体积归因把两者混为一谈。
    """
    by_role: dict[str, int] = {}
    total = 0
    for message in messages:
        line = json.dumps(message, ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        role = message.get("role") if isinstance(message, dict) else None
        if isinstance(role, str):
            key = role
        else:
            key = "<non-dict>" if not isinstance(message, dict) else "<no-role>"
        by_role[key] = by_role.get(key, 0) + size
        total += size
    return total, by_role


def audit_messages(
    messages: Sequence[Any],
    *,
    path: str | None = None,
    bytes_total: int | None = None,
    unparsable_lines: int = 0,
) -> AuditReport:
    """审计一份内存中的消息序列。

    Args:
        messages: 消息字典序列（可含非 dict 项，会报 V1）。
        path: 来源文件路径（仅用于报告标识）。
        bytes_total: 覆盖字节总量（文件入口用文件真实大小）；``None`` 时按消息行合计。
        unparsable_lines: 解析失败的原始行数（来自文件入口，仅作普查）。

    Returns:
        审计报告。纯函数，不改动入参。
    """
    violations = tuple(_audit_violations(messages))
    census = _audit_census(messages, unparsable_lines=unparsable_lines)
    line_bytes, bytes_by_role = _audit_bytes(messages)
    typed = [message for message in messages if isinstance(message, dict)]
    return AuditReport(
        path=path,
        message_count=len(messages),
        violations=violations,
        census=census,
        bytes_total=line_bytes if bytes_total is None else bytes_total,
        bytes_by_role=bytes_by_role,
        token_estimate=estimate_prompt_tokens(typed),
    )


def _parse_session_lines(lines: Iterable[str]) -> tuple[list[Any], int]:
    """解析会话 JSONL 行迭代器，返回 ``(消息列表, 不可解析行数)``。

    行切分口径与 :class:`SessionStore` 一致（文本模式按 ``\\n`` 迭代），**不可**用
    ``str.splitlines()``：它还会按 U+2028 / U+2029 / U+0085 切分，而
    ``json.dumps(ensure_ascii=False)`` 不转义这些码点，正文含这类字符时一条消息行会
    被切成两段、两段都解析失败，结果是"消息静默丢失"的漏报。

    - 首个可解析行若为 ``_type=metadata`` 的元数据行则跳过（按"首个非空行"判定，
      不按物理下标——外部编辑可能引入前导空行）；
    - 不可解析行按存储层语义跳过并计数，不阻断审计；
    - 合法 JSON 但**非对象**的行仍作为消息交给契约检查（会报 V1）：存储层对这类行是
      静默丢弃，审计侧显式报出，避免"悄悄少了一条消息"无人发现。
    """
    messages: list[Any] = []
    unparsable = 0
    first_payload_seen = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            unparsable += 1
            continue
        if not first_payload_seen:
            first_payload_seen = True
            if isinstance(payload, dict) and payload.get("_type") == "metadata":
                continue
        messages.append(payload)
    return messages, unparsable


def audit_session_file(path: str | Path) -> AuditReport:
    """审计一个会话 JSONL 文件（逐行流式读取，不在内存中放大整份文件）。

    Args:
        path: 会话文件路径。

    Returns:
        审计报告（``bytes_total`` 取文件真实字节数）。

    Raises:
        OSError: 文件不可读或读取中途失败（由调用方决定是跳过还是中止）。
    """
    target = Path(path)
    size = target.stat().st_size
    with open(target, encoding="utf-8", errors="replace") as handle:
        messages, unparsable = _parse_session_lines(handle)
    return audit_messages(
        messages,
        path=str(target),
        bytes_total=size,
        unparsable_lines=unparsable,
    )


def scan_session_dir(root: str | Path) -> list[AuditReport]:
    """递归审计目录下所有会话文件（跳过 consolidation 归档）。

    ``*.jsonl`` 模式不会匹配存储层的 ``xxx.jsonl.tmp`` 临时文件，故无需额外排除。

    Args:
        root: 起始目录。

    Returns:
        审计报告列表（不可读文件跳过并告警，不中断扫描）。
    """
    reports: list[AuditReport] = []
    base = Path(root)
    for candidate in sorted(base.rglob(f"*{_SESSION_SUFFIX}")):
        if candidate.name.endswith(_CONSOLIDATION_SUFFIX):
            continue
        try:
            reports.append(audit_session_file(candidate))
        except OSError as exc:
            logger.warning(f"会话审计跳过不可读文件 {candidate}: {exc}")
    return reports


def _format_report(report: AuditReport) -> str:
    """渲染单个文件的一行摘要。"""
    label = report.path or "<memory>"
    status = "OK" if report.ok else f"VIOLATIONS {len(report.violations)}"
    census = report.census
    return (
        f"[{status}] {label} | messages={report.message_count} "
        f"bytes={report.bytes_total} tokens={report.token_estimate} "
        f"protocol={census['protocol_rows']} truncated={census['truncated']} "
        f"cancelled={census['cancelled']} reasoning={census['reasoning_left']} "
        f"unredacted={census['unredacted']} unparsable={census['unparsable_lines']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：逐文件摘要 + 违规明细 + 退出码（合法 0 / 有违规或错误 1）。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(
        prog="python -m nanobee.session.session_audit",
        description="会话文件协议契约校验与度量（只读）",
        epilog="违规码：" + "；".join(f"{code}={text}" for code, text in _VIOLATION_MEANINGS.items()),
    )
    parser.add_argument("paths", nargs="+", help="会话 JSONL 文件，或包含会话文件的目录")
    args = parser.parse_args(argv)

    reports: list[AuditReport] = []
    failed_paths: list[str] = []
    for raw_path in args.paths:
        candidate = Path(raw_path)
        if candidate.is_dir():
            reports.extend(scan_session_dir(candidate))
        elif candidate.is_file():
            try:
                reports.append(audit_session_file(candidate))
            except OSError as exc:
                failed_paths.append(f"{candidate}: {exc}")
        else:
            failed_paths.append(f"{candidate}: 路径不存在")

    violation_total = 0
    for report in reports:
        print(_format_report(report))
        for violation in report.violations:
            print(f"    {violation.format()}")
        violation_total += len(report.violations)

    for failure in failed_paths:
        print(f"[ERROR] {failure}", file=sys.stderr)

    print(
        f"汇总：files={len(reports)} violations={violation_total} "
        f"errors={len(failed_paths)}",
    )
    return 1 if violation_total or failed_paths else 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = [
    "CANCELLED_TOOL_RESULT_CONTENT",
    "PERSIST_TRUNCATED_SUFFIX",
    "AuditReport",
    "Violation",
    "audit_messages",
    "audit_session_file",
    "main",
    "scan_session_dir",
]
