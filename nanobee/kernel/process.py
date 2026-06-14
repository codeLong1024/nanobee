"""进程管理相关工具

提供信号守卫（Signal Guard）功能，确保 Nanobee 进程在收到
终止信号时优雅退出。

遵循框架无知论：本模块只提供"收到信号后不要裸奔退出"的机制，
不持有任何策略（需要启动/停止多少实例、何时触发等）。
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

from nanobee.utils.logger import logger

if TYPE_CHECKING:
    from nanobee.kernel.kernel import NanobeeKernel


async def run_signal_guard(kernel: NanobeeKernel | None = None) -> None:
    """信号守卫：等待 SIGINT/SIGTERM，收到后调用 kernel.shutdown()。

    注册 SIGINT 和 SIGTERM 的 asyncio 信号处理器，阻塞直到收到
    任一终止信号，然后执行 kernel.shutdown() 完成优雅退出。

    Args:
        kernel: 可选的 NanobeeKernel 实例。传入时在收到信号后
                自动调用 kernel.shutdown()。

    Note:
        必须在主线程的 asyncio 事件循环中调用。
        若在非主线程或不支持 add_signal_handler 的平台上，
        会自动降级为不注册信号处理器（需依靠 KeyboardInterrupt 兜底）。
    """
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal() -> None:
        """信号处理回调，设置退出事件。"""
        if shutdown_event.is_set():
            return  # 防止重复触发
        logger.info("收到终止信号，正在优雅关闭...")
        shutdown_event.set()

    # 注册信号处理器
    registered = 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
            registered += 1
        except (ValueError, RuntimeError, NotImplementedError):
            logger.warning("无法注册信号 {} 处理器，跳过", sig.name)

    if registered == 0:
        logger.warning("未注册任何信号处理器，信号守卫降级为 KeyboardInterrupt 兜底")

    # 等待信号
    await shutdown_event.wait()

    # 传入 kernel 时自动执行优雅退出
    if kernel is not None:
        await kernel.shutdown()
