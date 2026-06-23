"""消息分发器 — 已废弃。

消息处理已统一到 kernel.handle_message() / kernel.inject_message() 单入口。
本文件保留占位以避免历史导入报错，将在后续版本中彻底移除。
"""

from __future__ import annotations


class MessageDispatcher:
    """[已废弃] 消息分发器 — 消息处理已统一到 Kernel 单入口。"""

    def __init__(self, *args, **kwargs) -> None:
        pass
