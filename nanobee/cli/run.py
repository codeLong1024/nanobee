"""
Nanobee Run - 轻量级 Agent CLI 模式

对应 nanobot 的 ``agent`` 命令设计哲学：
- 轻量级，仅启动 AgentLoop + 核心内核
- 不启动通道、Cron 等后台服务
- 支持单次消息模式（-m）和交互式模式
- 支持流式输出
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import click

from nanobee.bootstrap import bootstrap
from nanobee.config.loader import load_config
from nanobee.utils.observability import init_log_file_sink, setup_structured_logging

from nanobee.utils.logger import logger



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
    "-m", "--message",
    help="单次消息模式（非交互式）",
    default=None,
)
@click.option(
    "-s", "--session",
    "session_id",
    help="会话 ID",
    default="default",
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
def run(
    config: str | None,
    plugin_dir: str | None,
    message: str | None,
    session_id: str,
    verbose: bool,
    data_dir: str | None,
) -> None:
    """启动轻量级 Agent 会话（CLI 模式）

    不启动通道等后台服务。
    适用于开发调试和命令行交互。
    """
    log_level = logging.DEBUG if verbose else logging.WARNING
    setup_structured_logging(level=log_level)
    logger.debug("CLI run 命令已启动，verbose={}，session={}", verbose, session_id)

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

    # 创建内核并运行会话（轻量模式）
    _run_agent_session(cfg, plugin_dir, cfg.plugin_dirs, message, session_id)


def _run_agent_session(
    cfg: Any,
    plugin_dir: str | None,
    config_plugin_dirs: list[str] | None = None,
    message: str | None = None,
    session_id: str = "default",
) -> None:
    """运行 Agent 会话（轻量级，无后台服务）

    Args:
        cfg: 配置对象
        plugin_dir: 插件目录路径（命令行参数）
        config_plugin_dirs: 配置中的插件目录列表
        message: 单次消息（可选）
        session_id: 会话 ID
    """
    async def _run():
        # 确定插件目录
        effective_plugin_dirs = []
        if plugin_dir:
            effective_plugin_dirs = [plugin_dir]
        elif cfg.plugin_dirs:
            effective_plugin_dirs = list(cfg.plugin_dirs)
        else:
            effective_plugin_dirs = ["builtin", "plugins"]

        # 组合根启动（轻量模式：不启动通道）
        kernel = await bootstrap(
            config=cfg,
            model=cfg.agents.defaults.model,
            plugin_dirs=effective_plugin_dirs,
            start_services=False,
        )

        click.echo("🤖 Nanobee Agent 已启动")
        click.echo(f"  默认模型: {cfg.agents.defaults.model}")

        if message:
            # 单次消息模式
            response = await kernel.handle_message(message, context_id=session_id)
            if response and response.content:
                click.echo(f"\n🤖 Agent: {response.content}")
            await kernel.shutdown()
        else:
            # 交互式模式
            click.echo("\n输入消息开始对话 (输入 'quit' 或 'exit' 退出)\n")
            try:
                while True:
                    try:
                        user_input = click.prompt("👤 你", type=str)
                    except (EOFError, KeyboardInterrupt):
                        break

                    user_input = user_input.strip()
                    if not user_input:
                        continue
                    if user_input.lower() in ("quit", "exit", "退出"):
                        break

                    click.echo(f"\n🤖 Agent: ", nl=False)
                    try:
                        response = await kernel.handle_message(
                            user_input, context_id=session_id,
                        )
                        click.echo(response.content if response else "")
                    except RuntimeError as e:
                        click.echo(f"错误: {e}", err=True)
                    except Exception as e:
                        logger.exception("处理消息时发生错误")
                        click.echo(f"错误: {e}", err=True)

                    click.echo()
            finally:
                await kernel.shutdown()
                click.echo("\n👋 Agent 已停止")

    asyncio.run(_run())


def register(cli_group: click.Group) -> None:
    """注册到主 CLI"""
    cli_group.add_command(run)
