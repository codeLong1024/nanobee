"""多实例发现模块。

扫描 data_dir 下子目录，读取各实例 config.yaml，
解析实例名、端口、日志/PID 路径等信息。

遵循框架无知论：本模块只提供机制（目录扫描+配置读取），
不持有策略（哪些实例启用、如何分组）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger


# 默认值（当配置中未提供时回退）
_DEFAULT_PORT = 8080
_DEFAULT_LOG_NAME = "gateway-out.log"


@dataclass
class Instance:
    """网关实例描述。"""

    name: str  # 实例名称（data_dir 子目录名）
    config_path: Path  # 配置文件绝对路径
    port: int  # 网关监听端口
    log_path: Path  # 日志文件路径
    pid_name: str  # PID 文件标识名（SHA1 前 16 位）


class InstanceDiscovery:
    """多实例发现器。

    扫描 data_dir 下所有子目录，查找 config.yaml，
    解析 gateway.port 等信息。不递归多层级。
    """

    def discover(self, data_dir: Path) -> list[Instance]:
        """扫描 data_dir 发现所有 Gateway 实例。

        Args:
            data_dir: 数据根目录（如 /nanobee-data/）。

        Returns:
            Instance 列表，按实例名排序。
        """
        instances: list[Instance] = []
        if not data_dir.exists():
            logger.warning("Data directory does not exist: {}", data_dir)
            return instances

        for sub_dir in sorted(data_dir.iterdir()):
            if not sub_dir.is_dir():
                continue

            config_path = sub_dir / "config.yaml"
            if not config_path.is_file():
                continue

            try:
                inst = self._load_instance(sub_dir.name, config_path)
                if inst:
                    instances.append(inst)
            except (yaml.YAMLError, KeyError, ValueError) as e:
                logger.warning(
                    "Failed to parse config for instance {}: {}",
                    sub_dir.name,
                    e,
                )

        return instances

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _load_instance(self, name: str, config_path: Path) -> Instance | None:
        """从 config.yaml 加载单个实例描述。"""
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        gateway = config.get("gateway", {})
        port = gateway.get("port", _DEFAULT_PORT)
        if not isinstance(port, int):
            port = _DEFAULT_PORT

        # 日志路径
        log_dir = config_path.parent / "logs"
        log_path = log_dir / _DEFAULT_LOG_NAME

        # PID 文件名（基于配置路径 SHA1）
        pid_name = _instance_pid_name(config_path)

        return Instance(
            name=name,
            config_path=config_path,
            port=port,
            log_path=log_path,
            pid_name=pid_name,
        )


def _instance_pid_name(config_path: Path) -> str:
    """基于配置文件路径 SHA1 生成 PID 文件标识名。"""
    return hashlib.sha1(str(config_path).encode()).hexdigest()[:16]
