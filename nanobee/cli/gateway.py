"""
Nanobee Gateway - 完整服务栈模式

对应 nanobot 的 ``gateway`` 命令设计哲学：
- 启动完整服务栈：Kernel + 通道插件 + 健康端点
- 适用于生产部署
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import click

from nanobee.config.loader import load_config
from nanobee.kernel import NanobeeKernel
from nanobee.kernel.process import run_signal_guard
from nanobee.providers.factory import make_provider
from nanobee.utils.logger import logger
from nanobee.utils.observability import init_log_file_sink, setup_structured_logging



@click.command()
@click.option(
    "-c", "--config",
    type=click.Path(exists=True, dir_okay=False, file_okay=True),
    help="配置文件路径 (YAML 格式)",
)
@click.option(
    "-p", "--plugin-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="插件目录路径",
    default=None,
)
@click.option(
    "--port",
    type=int,
    help="健康检查 HTTP 端口（可选）",
    default=None,
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="显示详细日志",
)
def gateway(
    config: str | None,
    plugin_dir: str | None,
    port: int | None,
    verbose: bool,
) -> None:
    """启动 Gateway 服务（完整服务栈）

    启动所有已启用的通道插件和健康检查端点。
    适用于生产部署。
    """
    log_level = logging.DEBUG if verbose else logging.WARNING
    setup_structured_logging(level=log_level)
    logger.debug("Gateway 命令已启动，verbose={}", verbose)

    # 自动发现配置文件
    config_path: Path | None
    if config:
        config_path = Path(config)
    else:
        home_config = Path.home() / ".nanobee" / "nanobee.yaml"
        candidates = [home_config, Path("nanobee.yaml"), Path.cwd() / "nanobee.yaml"]
        config_path = next((p for p in candidates if p.is_file()), None)
        if config_path:
            logger.debug("Auto-discovered config: {}", config_path)
    cfg = load_config(config_path)

    # 根据配置添加 loguru 文件 sink（运行时日志自管理）
    log_cfg = getattr(cfg, "logging", None)
    if log_cfg is not None:
        init_log_file_sink(log_cfg.model_dump() if hasattr(log_cfg, "model_dump") else log_cfg)

    # 运行 Gateway 服务
    _run_gateway(cfg, plugin_dir, cfg.plugin_dirs, port=port)


def _run_gateway(
    cfg: Any,
    plugin_dir: str | None,
    config_plugin_dirs: list[str] | None = None,
    *,
    port: int | None = None,
) -> None:
    """运行 Gateway 服务（完整服务栈）

    Args:
        cfg: 配置对象
        plugin_dir: 插件目录路径（命令行参数）
        config_plugin_dirs: 配置中的插件目录列表
        port: 健康检查 HTTP 端口
    """
    async def _run():
        # 创建 provider
        provider = make_provider(cfg)
        click.echo(f"  Provider 已初始化: {provider.__class__.__name__}")

        # 确定插件目录
        kernel_config = dict(cfg)
        effective_plugin_dirs = []
        if plugin_dir:
            effective_plugin_dirs = [plugin_dir]
        elif config_plugin_dirs:
            effective_plugin_dirs = list(config_plugin_dirs)
        else:
            effective_plugin_dirs = ["builtin", "plugins"]

        # 创建内核
        kernel = NanobeeKernel(config=kernel_config, plugin_dirs=effective_plugin_dirs)
        await kernel.boot_with_provider(provider, model=cfg.agents.defaults.model)

        # 启动后台服务（通道插件）
        await kernel.boot_services()

        click.echo("🚪 Nanobee Gateway 已启动")
        click.echo(f"  默认模型: {cfg.agents.defaults.model}")
        click.echo(f"  配置提供者: {list(cfg.providers.keys())}")

        # 检查已启用的通道
        channels = kernel.plugin_manager.get_by_type("channel")
        if channels:
            names = [getattr(c, "name", type(c).__name__) for c in channels]
            click.echo(f"  已启用通道: {', '.join(names)}")
        else:
            click.echo("  [yellow]未启用通道[/yellow]")

        # 启动健康检查 HTTP 服务器（可选）
        health_port = port or _resolve_health_port(cfg)
        health_tasks = []
        if health_port:
            health_tasks.append(_health_server("127.0.0.1", health_port))
            click.echo(f"  健康端点: http://127.0.0.1:{health_port}/health")

        click.echo("")

        # 信号守卫：等待 SIGINT/SIGTERM 后优雅退出
        guard_task = asyncio.create_task(run_signal_guard())
        health_task_list = [asyncio.create_task(h) for h in health_tasks]

        try:
            done, pending = await asyncio.wait(
                [guard_task, *health_task_list],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except KeyboardInterrupt:
            click.echo("\n正在关闭 Gateway...")
        except Exception:
            logger.exception("Gateway 异常退出")
        finally:
            # 取消正在运行的健康检查任务
            for task in health_task_list:
                if not task.done():
                    task.cancel()
            if health_task_list:
                await asyncio.gather(*health_task_list, return_exceptions=True)
            await kernel.shutdown()
            click.echo("👋 Gateway 已停止")

    asyncio.run(_run())


async def _health_server(host: str, health_port: int) -> None:
    """轻量级 HTTP 健康端点"""
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


def _resolve_health_port(cfg: Any) -> int | None:
    """从配置中解析健康检查端口"""
    return getattr(cfg, "gateway", {}).get("port") if hasattr(cfg, "gateway") else None


def register(cli_group: click.Group) -> None:
    """注册到主 CLI"""
    cli_group.add_command(gateway)
