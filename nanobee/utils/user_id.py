"""user_id 存储键归一化（评审 #1 / #4 修复）。

叶子模块：仅依赖标准库与 ``nanobee.exceptions``，agent/session/kernel/
builtin 各层均可安全引用而不触发 kernel/agent 包初始化（规避循环导入）。

两层防线：
- **出生点归一**（:func:`resolve_storage_key`）：外部自由文本 id 在隔离键
  的出生点（``InboundMessage.context_id`` 属性、``_process_message`` 的
  key 派生）归一为可安全落盘的存储键——合法 id 原样返回（既有用户目录
  零迁移），白名单外的奇异 id 确定性哈希降级（可用性优先于拒绝）；
- **落点断言**（:func:`is_safe_user_id`）：``ContextManager`` /
  ``SessionStore`` / ``audit_logger`` 在拼接磁盘路径前断言，堵住未来
  绕过归一入口的调用方（如直接构造消息的测试或新入口）。
"""

from __future__ import annotations

import hashlib
import re

from nanobee.exceptions import ContextError
from nanobee.utils.logger import logger

# user_id 直接用于拼接磁盘目录（users_base_dir / user_id）及下游文件名
# （如 audit_logger/<user_id>.jsonl），必须拒绝路径分隔符与相对路径语义，
# 防止越界写（评审 F1）。长度上界取 64：覆盖 uuid4（36）、钉钉 staffId 等
# 现实形态，同时远小于文件系统单文件名 255 字节限制。
_USER_ID_MAX_LENGTH = 64
_USER_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

# 非法 id 的确定性哈希降级格式：纯白名单字符、无碰撞、同一 raw 恒同 key
# （会话连续性保留）。前缀 "u-" 防止与原始 id 形态混淆，便于日志甄别。
_HASH_PREFIX = "u-"
_HASH_LENGTH = 32

# 相对路径语义 / 非字符串属"错误输入"而非"奇异但合法"：fail-visible，
# 不做净化替换——净化会产生别名碰撞（如 ``a/b`` 与 ``a_b`` 映射到同一
# 目录），导致跨租户数据串写。
_RELATIVE_PATH_IDS = (".", "..")


def is_safe_user_id(raw: object) -> bool:
    """判定 raw 是否可直接用作存储键（目录名 / 文件名片段）。

    Args:
        raw: 待判定值（接受任意对象，非字符串直接返回 False）。

    Returns:
        仅含 ``[A-Za-z0-9._-]``、长度 1-64 且非 ``.`` / ``..`` 时 True。
    """
    return (
        isinstance(raw, str)
        and bool(raw)
        and raw not in _RELATIVE_PATH_IDS
        and len(raw) <= _USER_ID_MAX_LENGTH
        and _USER_ID_PATTERN.fullmatch(raw) is not None
    )


def resolve_storage_key(raw: str) -> str:
    """把外部来源的 user_id 归一化为可安全落盘的存储键。

    策略（评审 #4 拍板：可用性优先于拒绝）：
    - 合法 id：**原样返回**——既有用户目录（如 ``shenqla``）零迁移；
    - 白名单外的奇异 id（如钉钉加密形态 ``$:LWCP_v1:$...``、含 ``:``/
      ``/`` 的通道前缀 ``dingtalk:cid...``）：确定性映射为
      ``u-<sha256[:32]>``——同一 raw 恒同 key，目录/文件名安全且无碰撞；
    - ``.`` / ``..`` / 非字符串：raise :class:`ContextError`（错误输入，
      fail-visible）；空串原样透传——由 ``InboundMessage.context_id`` 的
      ``channel:chat_id`` 兜底分支接管后再归一。

    Args:
        raw: 外部来源的用户标识（通道 sender_id / conversation_id 等）。

    Returns:
        可安全用作磁盘目录名 / 文件名的存储键。

    Raises:
        ContextError: raw 非字符串，或为 ``.`` / ``..`` 相对路径语义。
    """
    if not isinstance(raw, str):
        raise ContextError(f"user_id 必须为字符串，实际为 {type(raw).__name__}")
    if raw in _RELATIVE_PATH_IDS:
        raise ContextError(f"user_id 不得为相对路径语义: {raw!r}")
    if is_safe_user_id(raw):
        return raw
    if raw == "":
        return ""
    hashed = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    # 告警只记长度，不记原值——敏感 id（加密用户标识）不得进入日志
    logger.warning(
        "user_id 含白名单外字符，已降级为确定性哈希存储键 (len={})", len(raw),
    )
    return f"{_HASH_PREFIX}{hashed}"
