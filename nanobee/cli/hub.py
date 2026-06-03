"""
Nanobee Hub - Hub 子命令
"""

import click


@click.group()
def hub():
    """插件市场相关命令"""
    pass


@hub.command()
@click.argument("query")
def search(query):
    """搜索插件"""
    click.echo(f"搜索: {query}")


@hub.command()
@click.argument("plugin_id")
def install(plugin_id):
    """安装插件"""
    click.echo(f"安装插件: {plugin_id}")


@hub.command()
@click.argument("plugin_id")
def uninstall(plugin_id):
    """卸载插件"""
    click.echo(f"卸载插件: {plugin_id}")


def register(cli_group):
    """注册到主 CLI"""
    cli_group.add_command(hub)
