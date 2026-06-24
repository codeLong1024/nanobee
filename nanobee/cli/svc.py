"""Nanobee Service - 多实例 Gateway 运行时管理。

提供 start/stop/restart/status/logs 五个子命令，
替代 deploy/nanobee-gateway.sh 的进程管理功能。

遵循框架无知论：CLI 层只负责参数解析和用户交互，
不包含任何策略决策。

配置入口：
  单实例场景：nanobee gateway -c <config.yaml>（不经过 svc）
  多实例场景：systemd 注入 NANOBEE_DATA_DIR 环境变量，
               svc start 扫描该目录下所有子目录的 config.yaml 并启动。
"""

from __future__ import annotations

import asyncio
import os

import click

from nanobee.gateway.runtime import GatewayRuntime
from nanobee.utils.logger import logger


def _build_runtime() -> GatewayRuntime:
    """构建 GatewayRuntime 实例。

    唯一入口：从 NANOBEE_DATA_DIR 环境变量读取数据目录。
    无该变量时提示用户配置 systemd 的 Environment= 行。
    """
    data_dir = os.environ.get("NANOBEE_DATA_DIR")
    if not data_dir:
        raise click.UsageError(
            "未设置 NANOBEE_DATA_DIR 环境变量。\n"
            "多实例管理需要在 systemd unit 的 [Service] 段中配置:\n"
            "  Environment=NANOBEE_DATA_DIR=/nanobee-data\n"
            "单实例场景请直接使用: nanobee gateway -c <config.yaml>"
        )
    return GatewayRuntime.create(data_dir)


def _run_async(coro):
    """在同步上下文中运行异步协程。"""
    return asyncio.run(coro)


@click.group(name="svc")
def svc():
    """Gateway 多实例运行时管理。

    用于管理 NANOBEE_DATA_DIR 下多个 Gateway 实例的
    启动、停止、重启、状态查看、日志读取。

    仅限多实例场景。单实例请直接用: nanobee gateway -c config.yaml
    """
    pass


@svc.command()
@click.argument("instance", required=False)
def start(instance: str | None) -> None:
    """启动一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认启动所有）。
    """
    runtime = _build_runtime()
    results = _run_async(runtime.start(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
@click.argument("instance", required=False)
def stop(instance: str | None) -> None:
    """停止一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认停止所有）。
    """
    runtime = _build_runtime()
    results = _run_async(runtime.stop(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
@click.argument("instance", required=False)
def restart(instance: str | None) -> None:
    """重启一个或所有 Gateway 实例。

    INSTANCE: 实例名称（可选，默认重启所有）。
    """
    runtime = _build_runtime()
    results = _run_async(runtime.restart(instance))

    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        click.echo(f"  {name}: {status}")


@svc.command()
def status() -> None:
    """查看所有 Gateway 实例的运行状态。"""
    runtime = _build_runtime()
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
@click.option("-n", "--lines", default=50, help="读取最后 N 行（默认 50）")
@click.option("-f", "--follow", is_flag=True, default=False, help="持续跟踪新内容")
@click.argument("instance")
def logs(lines: int, follow: bool, instance: str) -> None:
    """查看指定实例的日志。

    INSTANCE: 实例名称（必填）。
    """
    runtime = _build_runtime()
    content = _run_async(runtime.logs(instance, lines=lines, follow=follow))
    click.echo(content)

    if follow:
        try:
            _run_async(runtime.follow_logs(instance))
        except KeyboardInterrupt:
            pass


def register(cli_group: click.Group) -> None:
    """注册到主 CLI。"""
    cli_group.add_command(svc)
