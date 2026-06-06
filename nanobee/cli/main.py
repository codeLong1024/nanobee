"""
Nanobee CLI - 主命令入口
"""

import click

from nanobee.cli.gateway import register as register_gateway
from nanobee.cli.hub import register as register_hub
from nanobee.cli.plugin import register as register_plugin
from nanobee.cli.run import register as register_run


@click.group()
def main():
    """Nanobee - 极简 AI Agent 框架"""
    pass


if __name__ == "__main__":
    main()


# 注册子命令
register_gateway(main)
register_hub(main)
register_plugin(main)
register_run(main)
