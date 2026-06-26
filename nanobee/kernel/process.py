"""进程管理相关工具

提供信号守卫（Signal Guard）和 Gateway 生命周期管理功能，
确保 Nanobee 进程在收到终止信号时优雅退出。

遵循框架无知论：本模块只提供"收到信号后不要裸奔退出"的机制，
不持有任何策略（需要启动/停止多少实例、何时触发等）。
"""

from __future__ import annotations

import asyncio
import json
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


async def _health_server(host: str, health_port: int) -> None:
    """轻量级 HTTP 健康端点，返回 {"status": "ok"}。"""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return

        request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        parts = request_line.split(" ")
        method, path = ("", "")
        if len(parts) >= 2:
            method, path = parts[0], parts[1]

        if method == "GET" and path == "/health":
            body = json.dumps({"status": "ok"})
            resp = (
                f"HTTP/1.0 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n{body}"
            )
        else:
            body = "Not Found"
            resp = (
                f"HTTP/1.0 404 Not Found\r\n"
                f"Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n{body}"
            )

        writer.write(resp.encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, host, health_port)
    logger.info("健康端点已启动: http://{}:{}/health", host, health_port)
    async with server:
        await server.serve_forever()


async def _safe_health_server(host: str, health_port: int) -> None:
    """安全版本的健康端点，端口被占时不崩进程。"""
    try:
        await _health_server(host, health_port)
    except Exception:
        logger.exception("健康服务器启动失败（端口 {} 可能已被占用），进程继续运行", health_port)
        await asyncio.Event().wait()


async def run_gateway_lifecycle(
    kernel: NanobeeKernel,
    *,
    health_port: int | None = None,
) -> None:
    """Gateway 生命周期管理：健康服务器 + 信号守卫 + 优雅退出。

    运行健康检查 HTTP 服务器（可选），等待终止信号，
    收到信号后自动执行 kernel.shutdown()。

    Args:
        kernel: NanobeeKernel 实例
        health_port: 健康检查 HTTP 端口（可选，None 时不启动健康服务器）
    """
    # 健康服务器（可选）
    health_task: asyncio.Task | None = None
    if health_port:
        health_task = asyncio.create_task(
            _safe_health_server("127.0.0.1", health_port),
        )

    # 信号守卫（传入 kernel，收到信号后自动 shutdown）
    guard_task = asyncio.create_task(run_signal_guard(kernel))

    wait_tasks = [guard_task]
    if health_task:
        wait_tasks.append(health_task)

    try:
        done, pending = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Gateway 生命周期异常退出")
    finally:
        # 取消健康服务器任务（信号守卫已在内部执行 kernel.shutdown()）
        if health_task and not health_task.done():
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
