"""Gateway 多实例运行时管理器。

编排 PidManager、ProcessManager、HealthChecker、
InstanceDiscovery、LogReader 完成
start/stop/restart/status/logs 六大操作。

遵循框架无知论：本模块只编排已有机制，零触及 LLM 决策域。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from loguru import logger

from .discovery import InstanceDiscovery
from .health_checker import HealthChecker
from .log_reader import LogReader
from .pid_manager import PidManager
from .process_manager import ProcessManager


# 内部运行时常量（机制层参数，非用户可见策略）
_STOP_TIMEOUT = 20.0  # SIGTERM 后等待秒数
_HEALTH_CHECK_TIMEOUT = 10.0  # 健康检查总超时秒数
_HEALTH_CHECK_INTERVAL = 1.0  # 健康检查轮询间隔秒数
_RESTART_DELAY = 2.0  # restart 时 stop 与 start 之间的等待秒数


class GatewayRuntime:
    """Gateway 多实例运行时管理器。

    纯机制层：只负责进程生命周期、文件 I/O。
    零触及 LLM 决策域，符合框架无知论。
    单实例场景请直接用 nanobee gateway -c config.yaml，
    本模块仅处理 NANOBEE_DATA_DIR 下多实例的批量管理。
    """

    def __init__(
        self,
        data_dir: str,
        discovery: InstanceDiscovery,
        process_manager: ProcessManager,
        pid_manager: PidManager,
        health_checker: HealthChecker,
        log_reader: LogReader,
        venv_path: Path | None = None,
    ) -> None:
        """初始化运行时管理器。

        Args:
            data_dir: 数据根目录（实例目录的父目录）。
            discovery: 实例发现器。
            process_manager: 进程管理器。
            pid_manager: PID 文件管理器。
            health_checker: 健康检查器。
            log_reader: 日志读取器。
            venv_path: 虚拟环境根目录，None 时从 sys.executable 推导。
        """
        self._data_dir = Path(data_dir).expanduser()
        self._discovery = discovery
        self._process_manager = process_manager
        self._pid_manager = pid_manager
        self._health_checker = health_checker
        self._log_reader = log_reader
        self._venv_path = venv_path or Path(sys.executable).parent.parent

    @classmethod
    def create(cls, data_dir: str, venv_path: Path | None = None) -> GatewayRuntime:
        """工厂方法，自动创建所有子模块。

        Args:
            data_dir: 数据根目录（实例目录的父目录）。
            venv_path: 虚拟环境根目录，None 时从 sys.executable 推导。

        Returns:
            GatewayRuntime 实例。
        """
        pid_dir = str(Path(data_dir).expanduser() / ".pid")
        return cls(
            data_dir=data_dir,
            discovery=InstanceDiscovery(),
            process_manager=ProcessManager(),
            pid_manager=PidManager(pid_dir=Path(pid_dir)),
            health_checker=HealthChecker(),
            log_reader=LogReader(),
            venv_path=venv_path or Path(sys.executable).parent.parent,
        )

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
            results[inst.name] = await self._stop_one(inst)

        return results

    async def restart(self, name: str | None = None) -> dict[str, bool]:
        """重启一个或所有 Gateway 实例。

        先停止，等待 _RESTART_DELAY 秒，再启动。

        Args:
            name: 实例名称，None 表示重启所有。

        Returns:
            {实例名: 成功标志} 字典。
        """
        instances = self._resolve_instances(name)
        results: dict[str, bool] = {}

        for inst in instances:
            await self._stop_one(inst)
            await asyncio.sleep(_RESTART_DELAY)
            results[inst.name] = await self._start_one(inst)

        return results

    async def status(self) -> list[dict]:
        """查询所有实例的运行状态。

        Returns:
            状态字典列表，每项含 name、port、pid、running、pid_path 字段。
        """
        instances = self._discovery.discover(self._data_dir)
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
                "pid_path": str(self._pid_manager.pid_dir / f"{inst.pid_name}.pid"),
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
            日志内容字符串（follow 模式返回初始内容 + 持续输出新行到终端）。
        """
        instances = self._resolve_instances(name)
        if not instances:
            return f"No instance found: {name}"

        inst = instances[0]
        content = self._log_reader.tail(inst.log_path, lines=lines)

        if follow:
            # 副作用：调用 stream 将其内部偏移量初始化到文件末尾，
            # 丢弃返回值（此调用只为推进偏移，不读取已有内容）。
            # 后续 follow_logs 轮询时才会真正产出新增内容。
            self._log_reader.stream(inst.log_path)
            logger.info("Following logs for instance {} (Ctrl+C to stop)", name)

        return content or ""

    async def follow_logs(self, name: str, interval: float = 1.0) -> None:
        """持续跟踪实例日志（仅在 logs(follow=True) 后调用）。

        以 interval 秒间隔轮询新增日志，输出到 stderr。
        调用方捕获 KeyboardInterrupt 即可退出。

        Args:
            name: 实例名称。
            interval: 轮询间隔秒数。
        """
        instances = self._resolve_instances(name)
        if not instances:
            return

        inst = instances[0]
        try:
            while True:
                await asyncio.sleep(interval)
                new_content = self._log_reader.stream(inst.log_path)
                if new_content:
                    sys.stderr.write(new_content)
                    sys.stderr.flush()
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_instances(self, name: str | None):
        """解析要操作的实例列表。"""
        all_instances = self._discovery.discover(self._data_dir)
        if name:
            filtered = [i for i in all_instances if i.name == name]
            if not filtered:
                logger.warning("Instance '{}' not found", name)
            return filtered
        return all_instances

    async def _start_one(self, inst) -> bool:
        """启动单个实例。"""
        pid = self._pid_manager.read(inst.pid_name)
        if pid and self._process_manager.is_running(pid):
            logger.info("Instance {} already running (pid={})", inst.name, pid)
            return True

        try:
            venv_path = self._venv_path

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
                timeout=_HEALTH_CHECK_TIMEOUT,
                interval=_HEALTH_CHECK_INTERVAL,
            )

            if success:
                logger.info(
                    "Instance {} started successfully (pid={}, port={}, health_ok={:.2f}s)",
                    inst.name,
                    process.pid,
                    inst.port,
                    elapsed,
                )
                return True
            else:
                logger.warning(
                    "Instance {} started but health check failed (pid={}, port={})",
                    inst.name,
                    process.pid,
                    inst.port,
                )
                return False
        except (FileNotFoundError, OSError) as e:
            logger.exception("Failed to start instance {}", inst.name)
            return False

    async def _stop_one(self, inst) -> bool:
        """停止单个实例。

        process_manager.stop() 含同步轮询（time.sleep），
        通过 run_in_executor 在单独线程中执行，避免阻塞事件循环。
        """
        pid = self._pid_manager.read(inst.pid_name)
        if pid is None:
            logger.info("Instance {} not running (no PID file)", inst.name)
            return True

        if not self._process_manager.is_running(pid):
            logger.info("Instance {} already stopped (stale PID {})", inst.name, pid)
            self._pid_manager.remove(inst.pid_name)
            return True

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._process_manager.stop,
                pid,
                _STOP_TIMEOUT,
            )
            self._pid_manager.remove(inst.pid_name)
            logger.info("Instance {} stopped (was pid={})", inst.name, pid)
            return True
        except PermissionError:
            logger.exception("Failed to stop instance {} (pid={})", inst.name, pid)
            return False
