"""
Nanobee Run - Agent 运行命令
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import click

from nanobee.config.loader import load_config
from nanobee.kernel import NanobeeKernel
from nanobee.providers.factory import make_provider

logger = logging.getLogger(__name__)


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
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="显示详细日志",
)
def run(config: str | None, plugin_dir: str | None, verbose: bool) -> None:
    """启动 Agent 会话"""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # 自动发现配置文件：未指定时查找工作目录下的 nanobee.yaml
    config_path: Path | None
    if config:
        config_path = Path(config)
    else:
        candidates = [Path("nanobee.yaml"), Path.cwd() / "nanobee.yaml"]
        config_path = next((p for p in candidates if p.is_file()), None)
        if config_path:
            logger.debug("Auto-discovered config: %s", config_path)
    cfg = load_config(config_path)

    # 创建内核并运行会话
    _run_session(cfg, plugin_dir, cfg.plugin_dirs)


def _run_session(cfg: Any, plugin_dir: str | None, config_plugin_dirs: list[str] | None = None) -> None:
    """运行 Agent 会话（启动 + 交互循环）

    Args:
        cfg: 配置对象
        plugin_dir: 插件目录路径（命令行参数）
        config_plugin_dirs: 配置中的插件目录列表
    """
    async def _run():
        # 创建 provider
        provider = make_provider(cfg)
        click.echo(f"  Provider 已初始化: {provider.__class__.__name__}")

        # 创建内核
        kernel_config = dict(cfg)
        # 优先级：命令行参数 > 配置文件 > 默认值
        effective_plugin_dirs = []
        if plugin_dir:
            effective_plugin_dirs = [plugin_dir]
        elif config_plugin_dirs:
            effective_plugin_dirs = list(config_plugin_dirs)
        else:
            effective_plugin_dirs = ["builtin", "plugins"]
        
        kernel = NanobeeKernel(config=kernel_config, plugin_dirs=effective_plugin_dirs)

        # 使用 Provider 启动内核
        await kernel.boot_with_provider(provider, model=cfg.agents.defaults.model)

        click.echo("🤖 Nanobee Agent 已启动")
        click.echo(f"  默认模型: {cfg.agents.defaults.model}")
        click.echo(f"  配置提供者: {list(cfg.providers.keys())}")

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
                    response = await kernel.handle_message(user_input, context_id="default")
                    click.echo(response)
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
