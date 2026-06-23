"""systemd unit 渲染与安装模块。

使用 string.Template 渲染 systemd unit 文件，
不引入 Jinja2 等外部模板引擎依赖。
复用现有 deploy/nanobee-gateway.service 的安全加固配置。

遵循框架无知论：本模块只提供机制（模板渲染+文件写入），
不持有策略（何时安装、路径选择）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from string import Template

from loguru import logger

# systemd unit 模板（复用现有安全加固配置）
_UNIT_TEMPLATE = Template(
    """\
[Unit]
Description=Nanobee Gateway - $description
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
Group=$group
ExecStart=$exec_start
ExecStop=$exec_stop
ExecReload=$exec_stop && sleep 2 && $exec_start
Restart=always
RestartSec=10s

# 安全加固
ProtectSystem=full
ReadWritePaths=$data_dir
PrivateTmp=yes
NoNewPrivileges=yes
ProtectHome=read-only
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# 日志
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nanobee-$instance

[Install]
WantedBy=multi-user.target
"""
)


class SystemdRenderer:
    """systemd unit 渲染器。

    根据实例配置渲染 systemd unit 文件，
    支持安装到 /etc/systemd/system/。
    """

    def __init__(self, binary_path: str = "nanobee") -> None:
        """初始化渲染器。

        Args:
            binary_path: nanobee 命令路径（默认使用系统 PATH）。
        """
        self._binary_path = binary_path

    def render(self, instance_name: str, config_path: Path, data_dir: Path) -> str:
        """渲染 systemd unit 文件内容。

        Args:
            instance_name: 实例名称。
            config_path: 配置文件路径。
            data_dir: 数据目录路径。

        Returns:
            systemd unit 文件的完整内容。
        """
        return _UNIT_TEMPLATE.substitute(
            description=f"instance {instance_name}",
            user="nanobee",
            group="nanobee",
            exec_start=f"{self._binary_path} svc start {instance_name}",
            exec_stop=f"{self._binary_path} svc stop {instance_name}",
            data_dir=str(data_dir),
            instance=instance_name,
        )

    def install(self, instance_name: str, config_path: Path, data_dir: Path) -> Path:
        """渲染并安装 systemd unit 文件。

        写入 /etc/systemd/system/nanobee-<instance>.service。
        安装后需手动执行 systemctl daemon-reload。

        Args:
            instance_name: 实例名称。
            config_path: 配置文件路径。
            data_dir: 数据目录路径。

        Returns:
            写入的 unit 文件路径。
        """
        content = self.render(instance_name, config_path, data_dir)

        unit_name = f"nanobee-{instance_name}.service"
        unit_path = Path("/etc/systemd/system") / unit_name

        # 原子写入：先写临时文件再 rename
        tmp_path = unit_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content)
            tmp_path.chmod(0o644)
            shutil.move(str(tmp_path), str(unit_path))
        except OSError:
            logger.exception("Failed to install systemd unit: {}", unit_path)
            raise

        logger.info(
            "Installed systemd unit: {} (run: systemctl daemon-reload && systemctl start {})",
            unit_path,
            unit_name.replace(".service", ""),
        )
        return unit_path
