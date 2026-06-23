"""Gateway 多实例运行时管理器。

编排 PidManager、ProcessManager、HealthChecker、
InstanceDiscovery、LogReader、SystemdRenderer 完成
start/stop/restart/status/logs/install 六大操作。

遵循框架无知论：本模块只编排已有机制，零触及 LLM 决策域。
所有阈值从 GatewayConfig 读取，零硬编码。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from nanobee.config.schema import GatewayConfig

from .discovery import InstanceDiscovery
from .health_checker import HealthChecker
from .log_reader import LogReader
from .pid_manager import PidManager
from .process_manager import ProcessManager
from .systemd_renderer import SystemdRenderer


class GatewayRuntime:
    """Gateway 多实例运行时管理器。

    纯机制层：只负责进程生命周期、文件 I/O、系统集成。
    零触及 LLM 决策域，符合框架无知论。
    """

    def __init__(
        self,
        config: GatewayConfig,
        discovery: InstanceDiscovery,
        process_manager: ProcessManager,
        pid_manager: PidManager,
        health_checker: HealthChecker,
        log_reader: LogReader,
        systemd_renderer: SystemdRenderer,
    ) -> None:
        """初始化运行时管理器。

        Args:
            config: Gateway 运行时配置（超时、间隔等）。
            discovery: 实例发现器。
            process_manager: 进程管理器。
            pid_manager: PID 文件管理器。
            health_checker: 健康检查器。
            log_reader: 日志读取器。
            systemd_renderer: systemd unit 渲染器。
        """
        self._config = config
        self._discovery = discovery
        self._process_manager = process_manager
        self._pid_manager = pid_manager
        self._health_checker = health_checker
        self._log_reader = log_reader
        self._systemd_renderer = systemd_renderer

    async def start(self, name: str | None = None) -> dict[str, bool]:
        """启动一个或所有 Gateway 实例。

        Args:
            name: 实例名称，None 表示启动所有。

        Returns:
            {实例名: 成功标志} 字典。
        """
        instances = self._resolve_instances(name)
        results: dict[str, bool] = {}

        for inst in instances:
            results[inst.name] = await self._start_one(inst)

        return results

    async def stop(self, name: str | None = None) -> dict[str, bool]:
        """停止一个或所有 Gateway 实例。

        Args:
            name: 实例名称，None 表示停止所有。

        Returns:
            {实例名: 成功标志} 字典。
        """
        instances = self._resolve_instances(name)
        results: dict[str, bool] = {}

        for inst in instances:
            results[inst.name] = self._stop_one(inst)

        return results

    async def restart(self, name: str | None = None) -> dict[str, bool]:
        """重启一个或所有 Gateway 实例。

        先停止，等待 restart_delay 秒，再启动。

        Args:
            name: 实例名称，None 表示重启所有。

        Returns:
            {实例名: 成功标志} 字典。
        """
        instances = self._resolve_instances(name)
        results: dict[str, bool] = {}

        for inst in instances:
            self._stop_one(inst)
            await asyncio.sleep(self._config.restart_delay)
            results[inst.name] = await self._start_one(inst)

        return results

    async def status(self) -> list[dict]:
        """查询所有实例的运行状态。

        Returns:
            状态字典列表，每项含 name、port、pid、running、pid_path 字段。
        """
        instances = self._discovery.discover(self._resolve_data_dir())
        status_list: list[dict] = []

        for inst in instances:
            pid = self._pid_manager.read(inst.pid_name)
            running = self._process_manager.is_running(pid) if pid else False

            # Stale PID 清理：PID 文件存在但进程已死
            if pid and not running:
                logger.info("Cleaning stale PID for instance {}", inst.name)
                self._pid_manager.remove(inst.pid_name)
                pid = None

            status_list.append({
                "name": inst.name,
                "port": inst.port,
                "pid": pid,
                "running": running,
                "pid_path": str(self._pid_manager._pid_dir / f"{inst.pid_name}.pid"),
                "config_path": str(inst.config_path),
            })

        return status_list

    async def logs(self, name: str, lines: int = 50, follow: bool = False) -> str:
        """读取指定实例的日志。

        Args:
            name: 实例名称。
            lines: 读取最后 N 行。
            follow: 是否持续跟踪新内容。

        Returns:
            日志内容字符串。
        """
        instances = self._resolve_instances(name)
        if not instances:
            return f"No instance found: {name}"

        inst = instances[0]
        content = self._log_reader.tail(inst.log_path, lines=lines)

        if follow and content is not None:
            logger.info("Following logs for instance {} (Ctrl+C to stop)", name)
            # 仅返回当前内容，后续跟踪由 CLI 层处理

        return content or ""

    def install(self, name: str | None = None) -> dict[str, Path]:
        """安装 systemd unit 文件。

        Args:
            name: 实例名称，None 表示安装所有。

        Returns:
            {实例名: unit 文件路径} 字典。
        """
        instances = self._resolve_instances(name)
        results: dict[str, Path] = {}

        for inst in instances:
            unit_path = self._systemd_renderer.install(
                instance_name=inst.name,
                config_path=inst.config_path,
                data_dir=inst.config_path.parent,
            )
            results[inst.name] = unit_path

        return results

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_instances(self, name: str | None):
        """解析要操作的实例列表。"""
        all_instances = self._discovery.discover(self._resolve_data_dir())
        if name:
            filtered = [i for i in all_instances if i.name == name]
            if not filtered:
                logger.warning("Instance '{}' not found", name)
            return filtered
        return all_instances

    def _resolve_data_dir(self) -> Path:
        """解析 data_dir 路径。"""
        # 从第一个发现的实例的 config_path 反推
        # 实际使用时需要显式传入，这里用延迟解析
        return Path("/nanobee-data")  # 默认值，实际由 CLI 层传入

    async def _start_one(self, inst) -> bool:
        """启动单个实例。"""
        pid = self._pid_manager.read(inst.pid_name)
        if pid and self._process_manager.is_running(pid):
            logger.info("Instance {} already running (pid={})", inst.name, pid)
            return True

        try:
            # 解析 venv 路径（与项目 .venv 相同）
            venv_path = self._resolve_venv_path()

            process = self._process_manager.start(
                config_path=inst.config_path,
                venv_path=venv_path,
                log_path=inst.log_path,
            )

            # 写入 PID
            self._pid_manager.write(inst.pid_name, process.pid)

            # 健康检查
            success, elapsed = await self._health_checker.poll(
                port=inst.port,
                timeout=self._config.health_check_timeout,
                interval=self._config.health_check_interval,
            )

            if success:
                logger.info(
                    "Instance {} started successfully (pid={}, port={}, health_ok={:.2f}s)",
                    inst.name,
                    process.pid,
                    inst.port,
                    elapsed,
                )
            else:
                logger.warning(
                    "Instance {} started but health check failed (pid={}, port={})",
                    inst.name,
                    process.pid,
                    inst.port,
                )

            return True
        except (FileNotFoundError, OSError) as e:
            logger.exception("Failed to start instance {}", inst.name)
            return False

    def _stop_one(self, inst) -> bool:
        """停止单个实例。"""
        pid = self._pid_manager.read(inst.pid_name)
        if pid is None:
            logger.info("Instance {} not running (no PID file)", inst.name)
            return True

        if not self._process_manager.is_running(pid):
            logger.info("Instance {} already stopped (stale PID {})", inst.name, pid)
            self._pid_manager.remove(inst.pid_name)
            return True

        try:
            self._process_manager.stop(pid=pid, timeout=self._config.stop_timeout)
            self._pid_manager.remove(inst.pid_name)
            logger.info("Instance {} stopped (was pid={})", inst.name, pid)
            return True
        except PermissionError:
            logger.exception("Failed to stop instance {} (pid={})", inst.name, pid)
            return False

    def _resolve_venv_path(self) -> Path:
        """解析虚拟环境路径。

        默认使用项目根目录下的 .venv。
        """
        # 从当前文件路径推导项目根目录
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent
        return project_root / ".venv"
