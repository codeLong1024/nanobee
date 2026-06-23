"""
Nanobee CLI - 主命令入口
"""

import click

from nanobee.cli.gateway import register as register_gateway
from nanobee.cli.plugin import register as register_plugin
from nanobee.cli.run import register as register_run
from nanobee.cli.svc import register as register_svc


def _get_version() -> str:
    """从包元数据获取版本号。"""
    try:
        from importlib.metadata import version
        return version("nanobee")
    except Exception:
        return "0.1.0"


@click.group()
@click.version_option(version=_get_version(), prog_name="nanobee")
def main():
    """Nanobee - 极简 AI Agent 框架"""
    pass


if __name__ == "__main__":
    main()


# 注册子命令
register_gateway(main)
register_plugin(main)
register_run(main)
register_svc(main)
