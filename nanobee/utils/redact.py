"""错误文本归一化与凭证脱敏 — 异常串离开进程前的单一处理点。

背景：错误诊断串有多个产生点（provider 响应正文、runner 兜底、状态机兜底、
内核兜底），又有多个离开进程的出口（用户可见系统通知的 ``{detail}`` 与
``metadata["error_detail"]``、审计 JSONL、运行日志）。若按出口逐个脱敏，
每新增一个出口都必须记得补一次，必然遗漏（2026-09-13 评审实证：审计侧已
脱敏而用户可见侧裸串透传，即此缺陷）。故收敛到「错误串出生点」——凡把异常
或诊断串落成文本的地方统一调用 :func:`normalize_error`，下游出口自动继承。

职责边界（框架无知论）：本模块只提供机制（前缀归一 + 凭证掩码），不决定
何时上报、向用户展示什么文案（文案归通知目录）。

叶子模块：仅依赖标准库 ``re``，可被任意层安全导入。
"""

from __future__ import annotations

import re

# 凭证键名（键名保留以维持诊断价值，仅掩码其值）。
# 含 snake_case 组合名（client_secret 等）由「前置非字母数字」约束 + 短名兜底覆盖，
# 故 db_password / refresh_token 一类的复合键无需逐一枚举。
_SECRET_KEYS = (
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret"
    r"|secret|password|passwd|token|key|authorization"
)

# 凭证掩码正则。相对初版（audit_logger 内私有实现）的两处修正：
#   1. 前置边界由 ``\b`` 改为 ``[^A-Za-z0-9]``——``\b`` 在 ``_`` 两侧不成立，
#      导致 ``client_secret=`` / ``db_password=`` 等 snake_case 键失配（漏掩码）；
#   2. 键名与值均允许可选的单/双引号包裹——JSON 形态 ``"api_key": "sk-x"``
#      在初版下失配（漏掩码）。
# 值边界止于空白/&/引号/逗号，兼容 URL query（?key=x&next=1）、"k: v"、
# JSON（"k":"v",）三种形态。
_SECRET_PATTERN = re.compile(
    rf"((?:^|[^A-Za-z0-9])(?:{_SECRET_KEYS})[\"']?\s*[=:]\s*(?:bearer\s+)?[\"']?)"
    r"([^\s&\"',]+)",
    re.IGNORECASE,
)

# 错误串的既有前缀（大小写不敏感）。provider 层用 ``content="Error: ..."``
# 承载错误（openai_compat/anthropic/azure/bedrock 诸实现），其 content 另有
# 独立消费者，前缀不可在该层删除；故在归一化时统一剥离后重建，保证同一
# 错误在用户通知、审计、日志三处形态一致。
_ERROR_PREFIX_PATTERN = re.compile(r"^\s*(?:error|exception)\s*[:：]\s*", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """掩码文本中的凭证值（键名保留，值替换为 ``<redacted>``）。

    Args:
        text: 待处理文本（错误诊断、工具参数/结果预览等）。

    Returns:
        脱敏后的文本；无凭证形态时原样返回。
    """
    return _SECRET_PATTERN.sub(r"\1<redacted>", text)


def normalize_error(value: str | BaseException) -> str:
    """把任意来源的错误诊断归一为统一形态并脱敏。

    统一形态为 ``<异常类型>: <诊断正文>``（**不带** ``Error:`` 英文前缀）：
    用户可见模板是中文致歉文案，前缀属噪音，且多数异常类名本身已含 ``Error``。

    接受的输入包含两类来源：
    - 异常对象（runner/loop/kernel 兜底路径）；
    - 已有文本（provider 响应正文经 ``hook.finalize_content`` 的产物、
      插件注入的 ``AgentRunSpec.error_message``），此时先剥离既有前缀再重建。

    脱敏与归一是同一动作：凡是离开进程的错误串都经此函数，下游出口
    （通知 content、metadata.error_detail、审计、日志）无需各自处理。

    Args:
        value: 异常对象，或已是文本的诊断串（可带 ``Error:`` 前缀）。

    Returns:
        归一化并脱敏后的诊断串；输入为空文本时返回空串（调用方自行兜底文案）。
    """
    if isinstance(value, BaseException):
        detail = str(value)
        text = f"{type(value).__name__}: {detail}" if detail else type(value).__name__
    else:
        text = _ERROR_PREFIX_PATTERN.sub("", str(value)).strip()
    return redact_secrets(text)
