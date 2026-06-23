"""健康检查轮询模块。

异步轮询 Gateway 的 /health 端点，
在超时限制内等待 Gateway 就绪。

遵循框架无知论：本模块只提供机制（轮询 HTTP 端点），
不持有策略（检查路径、成功判定标准）。
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
from loguru import logger


class HealthChecker:
    """异步健康检查轮询器。

    轮询 GET /health 端点，期待返回 HTTP 200。
    连接异常时自动重试（视为未就绪），非 200 状态码视为失败。
    """

    async def poll(
        self,
        port: int,
        timeout: float,
        interval: float,
    ) -> tuple[bool, float]:
        """异步轮询健康检查直到成功或超时。

        Args:
            port: Gateway 监听端口。
            timeout: 总超时秒数。
            interval: 两次轮询之间的间隔秒数。

        Returns:
            (成功标志, 实际耗时秒数) 元组。
        """
        url = f"http://127.0.0.1:{port}/health"
        start = time.monotonic()
        deadline = start + timeout

        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            elapsed = time.monotonic() - start
                            logger.info(
                                "Health check passed on port {} after {:.2f}s",
                                port,
                                elapsed,
                            )
                            return True, elapsed
                except (aiohttp.ClientError, ConnectionRefusedError, OSError):
                    # 连接被拒绝视为尚未就绪，继续重试
                    pass

                await asyncio.sleep(interval)

        # 超时
        elapsed = time.monotonic() - start
        logger.warning(
            "Health check timeout on port {} after {:.2f}s",
            port,
            elapsed,
        )
        return False, elapsed
