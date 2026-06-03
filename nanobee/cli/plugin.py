"""
Nanobee Plugin - Plugin 子命令
"""

import click


@click.group()
def plugin():
    """插件管理命令"""
    pass


@plugin.command()
@click.argument("name")
def create(name):
    """创建新插件"""
    click.echo(f"创建插件: {name}")


@plugin.command()
def list():
    """列出已安装插件"""
    click.echo("已安装插件列表")


@plugin.command()
@click.argument("name")
def enable(name):
    """启用插件"""
    click.echo(f"启用插件: {name}")


@plugin.command()
@click.argument("name")
def disable(name):
    """禁用插件"""
    click.echo(f"禁用插件: {name}")


def register(cli_group):
    """注册到主 CLI"""
    cli_group.add_command(plugin)
