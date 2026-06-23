"""Nanobee Service - 多实例 Gateway 运行时管理。

提供 start/stop/restart/status/logs/install 六个子命令，
替代 deploy/nanobee-gateway.sh 的进程管理功能。

遵循框架无知论：CLI 层只负责参数解析和用户交互，
不包含任何策略决策。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from nanobee.config.loader import load_config
from nanobee.config.schema import GatewayConfig
from nanobee.gateway.discovery import InstanceDiscovery
from nanobee.gateway.health_checker import HealthChecker
from nanobee.gateway.log_reader import LogReader
from nanobee.gateway.pid_manager import PidManager
from nanobee.gateway.process_manager import ProcessManager
from nanobee.gateway.runtime import GatewayRuntime
from nanobee.gateway.systemd_renderer import SystemdRenderer
from nanobee.utils.logger import logger


def _build_runtime(config_path: str | None = None) -> GatewayRuntime:
    """根据配置文件构建 GatewayRuntime 实例。

    Args:
        config_path: 配置文件路径，None 时自动搜索。

    Returns:
        GatewayRuntime 实例。
    """
    cfg = load_config(config_path)
    runtime_config = cfg.gateway

    # 解析 PID 目录
    pid_dir = runtime_config.pid_dir or str(Path(cfg.data_dir).expanduser() / ".pid")
    pid_manager = PidManager(pid_dir=Path(pid_dir))

    discovery = InstanceDiscovery()
    process_manager = ProcessManager()
    health_checker = HealthChecker()
    log_reader = LogReader()
    systemd_renderer = SystemdRenderer()

    return GatewayRuntime(
        config=runtime_config,
        discovery=discovery,
        process_manager=process_manager,
        pid_manager=pid_manager,
        health_checker=health_checker,
        log_reader=log_reader,
        systemd_renderer=systemd_renderer,
    )


def _run_async(coro):
    """在同步上下文中运行异步协程。"""
    return asyncio.run(coro)


@click.group(name="svc")
def svc():
    """Gateway 多实例运行时管理。

    用于管理多个 Gateway 实例的启动、停止、重启、状态查看、
    日志读取和 systemd 服务安装。
    """
    pass


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.argument("instance", required=False)
def start(config_path: str | None, instance: str | None) -> None:
    """启动一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认启动所有）。
    """
    runtime = _build_runtime(config_path)
    results = _run_async(runtime.start(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.argument("instance", required=False)
def stop(config_path: str | None, instance: str | None) -> None:
    """停止一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认停止所有）。
    """
    runtime = _build_runtime(config_path)
    results = _run_async(runtime.stop(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.argument("instance", required=False)
def restart(config_path: str | None, instance: str | None) -> None:
    """重启一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认重启所有）。
    """
    runtime = _build_runtime(config_path)
    results = _run_async(runtime.restart(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
def status(config_path: str | None) -> None:
    """查看所有 Gateway 实例的运行状态。"""
    runtime = _build_runtime(config_path)
    statuses = _run_async(runtime.status())

    if not statuses:
        click.echo("No instances found.")
        return

    # 格式化表格输出
    click.echo(f"{'NAME':<20} {'PORT':<8} {'PID':<10} {'STATUS':<12} {'CONFIG'}")
    click.echo("-" * 80)
    for s in statuses:
        status_str = "RUNNING" if s["running"] else "STOPPED"
        pid_str = str(s["pid"]) if s["pid"] else "-"
        click.echo(
            f"{s['name']:<20} {s['port']:<8} {pid_str:<10} "
            f"{status_str:<12} {s['config_path']}"
        )


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.option("-n", "--lines", default=50, help="读取最后 N 行（默认 50）")
@click.option("-f", "--follow", is_flag=True, default=False, help="持续跟踪新内容")
@click.argument("instance")
def logs(config_path: str | None, lines: int, follow: bool, instance: str) -> None:
    """查看指定实例的日志。

    INSTANCE: 实例名称（必填）。
    """
    runtime = _build_runtime(config_path)
    content = _run_async(runtime.logs(instance, lines=lines, follow=follow))
    click.echo(content)


@svc.command()
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.argument("instance", required=False)
def install(config_path: str | None, instance: str | None) -> None:
    """安装 systemd unit 文件。

    INSTANCE: 实例名称（可选，默认安装所有）。
    安装后请手动执行: systemctl daemon-reload
    """
    runtime = _build_runtime(config_path)
    results = runtime.install(instance)

    for name, unit_path in results.items():
        click.echo(f"  {name}: {unit_path}")

    click.echo("\nRun: systemctl daemon-reload")
    click.echo("Then: systemctl start nanobee-<instance>")


def register(cli_group: click.Group) -> None:
    """注册到主 CLI。"""
    cli_group.add_command(svc)
