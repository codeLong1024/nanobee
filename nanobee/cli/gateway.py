"""
Nanobee Gateway - 完整服务栈模式

对应 nanobot 的 ``gateway`` 命令设计哲学：
- 启动完整服务栈：Kernel + 通道插件 + 健康端点
- 适用于生产部署
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import click

from nanobee.bootstrap import bootstrap
from nanobee.config.loader import load_config
from nanobee.kernel.process import run_gateway_lifecycle
from nanobee.utils.logger import logger
from nanobee.utils.observability import init_log_file_sink, setup_structured_logging



@click.command()
@click.option(
    "-c", "--config",
    type=click.Path(dir_okay=False, file_okay=True),
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
@click.option(
    "--data-dir",
    type=str,
    help="覆盖配置中的 data_dir",
    default=None,
)
def gateway(
    config: str | None,
    plugin_dir: str | None,
    port: int | None,
    verbose: bool,
    data_dir: str | None,
) -> None:
    """启动 Gateway 服务（完整服务栈）

    启动所有已启用的通道插件和健康检查端点。
    适用于生产部署。
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    setup_structured_logging(level=log_level)
    logger.debug("Gateway 命令已启动，verbose={}", verbose)

    # 自动发现配置文件（仅搜索当前目录，不硬编码 ~/.nanobee）
    config_path: Path | None
    if config:
        config_path = Path(config)
    else:
        candidates = [Path("nanobee.yaml"), Path.cwd() / "nanobee.yaml"]
        config_path = next((p for p in candidates if p.is_file()), None)
        if config_path:
            logger.debug("Auto-discovered config: {}", config_path)
    cfg = load_config(config_path)

    # 命令行 --data-dir 覆盖配置
    if data_dir is not None:
        cfg.data_dir = data_dir

    # 根据配置添加 loguru 文件 sink（运行时日志自管理）
    log_cfg = getattr(cfg, "logging", None)
    if log_cfg is not None:
        init_log_file_sink(log_cfg.model_dump() if hasattr(log_cfg, "model_dump") else log_cfg)
    # 运行 Gateway 服务
    _run_gateway(cfg, plugin_dir, port=port)


def _run_gateway(
    cfg: Any,
    plugin_dir: str | None = None,
    *,
    port: int | None = None,
) -> None:
    """运行 Gateway 服务（完整服务栈）

    Arg:
        cfg: 配置对象
        plugin_dir: 插件目录路径（命令行参数）
        port: 健康检查 HTTP 端口
    """
    async def _run():
        # 插件目录：仅传递 CLI --plugin-dir 覆盖，其余由内核自动发现
        effective_plugin_dirs = [plugin_dir] if plugin_dir else None

        logger.debug("插件目录: {}", effective_plugin_dirs)

        # 组合根启动（完整服务栈：含通道）
        kernel = await bootstrap(
            config=cfg,
            model=cfg.agents.defaults.model,
            plugin_dirs=effective_plugin_dirs,
            start_services=True,
        )

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
        if health_port:
            click.echo(f"  健康端点: http://127.0.0.1:{health_port}/health")
        click.echo("")

        # 生命周期管理：健康服务器 + 信号守卫 + 优雅退出
        await run_gateway_lifecycle(kernel, health_port=health_port)

    asyncio.run(_run())


def _resolve_health_port(cfg: Any) -> int | None:
    """从配置中解析健康检查端口"""
    gw = getattr(cfg, "gateway", None)
    return gw.port if gw is not None else None


def register(cli_group: click.Group) -> None:
    """注册到主 CLI"""
    cli_group.add_command(gateway)
