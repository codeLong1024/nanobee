"""GatewayRuntime 完整测试套件。

覆盖 PidManager、ProcessManager、HealthChecker、InstanceDiscovery、
LogReader、GatewayRuntime 编排层、CLI svc 命令。

遵循 TDD 红-绿-重构循环：先写测试，再写实现。
"""

from __future__ import annotations

import os
import signal
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import pytest_asyncio

from nanobee.gateway.discovery import Instance, InstanceDiscovery
from nanobee.gateway.health_checker import HealthChecker
from nanobee.gateway.log_reader import LogReader
from nanobee.gateway.pid_manager import PidManager
from nanobee.gateway.process_manager import ProcessManager
from nanobee.gateway.runtime import GatewayRuntime


class TestPidManager:
    """PID 文件原子操作测试。"""

    @pytest.fixture
    def tmp_dir(self):
        """创建临时目录用于 PID 文件操作。"""
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_write_and_read_pid(self, tmp_dir):
        """写入 PID 文件后应能正确读取。"""
        pm = PidManager(pid_dir=tmp_dir)
        pm.write("instance1", 12345)

        assert pm.read("instance1") == 12345
        assert (tmp_dir / "instance1.pid").exists()

    def test_write_pid_atomic_no_partial_file(self, tmp_dir):
        """原子写入应不产生不完整的 PID 文件。"""
        pm = PidManager(pid_dir=tmp_dir)
        pm.write("instance1", 12345)

        # 验证文件内容完整
        pid_path = tmp_dir / "instance1.pid"
        content = pid_path.read_text().strip()
        assert content == "12345"
        # 不应有临时文件残留
        tmp_files = list(tmp_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_write_overwrites_existing_pid(self, tmp_dir):
        """再次写入相同实例应覆盖旧 PID。"""
        pm = PidManager(pid_dir=tmp_dir)
        pm.write("instance1", 12345)
        pm.write("instance1", 67890)

        assert pm.read("instance1") == 67890

    def test_read_nonexistent_pid(self, tmp_dir):
        """读取不存在的 PID 文件应返回 None。"""
        pm = PidManager(pid_dir=tmp_dir)
        assert pm.read("nonexistent") is None

    def test_read_invalid_pid_content(self, tmp_dir):
        """PID 文件内容非数字时应返回 None。"""
        pid_path = tmp_dir / "bad.pid"
        pid_path.write_text("not-a-number")

        pm = PidManager(pid_dir=tmp_dir)
        assert pm.read("bad") is None

    def test_read_empty_pid_file(self, tmp_dir):
        """空 PID 文件应返回 None。"""
        pid_path = tmp_dir / "empty.pid"
        pid_path.write_text("")

        pm = PidManager(pid_dir=tmp_dir)
        assert pm.read("empty") is None

    def test_read_whitespace_only_pid_file(self, tmp_dir):
        """仅含空白字符的 PID 文件应返回 None。"""
        pid_path = tmp_dir / "whitespace.pid"
        pid_path.write_text("  \n  ")

        pm = PidManager(pid_dir=tmp_dir)
        assert pm.read("whitespace") is None

    def test_remove_pid(self, tmp_dir):
        """删除 PID 文件后应不可读取。"""
        pm = PidManager(pid_dir=tmp_dir)
        pm.write("instance1", 12345)
        pm.remove("instance1")

        assert pm.read("instance1") is None
        assert not (tmp_dir / "instance1.pid").exists()

    def test_remove_nonexistent_no_error(self, tmp_dir):
        """删除不存在的 PID 文件不应报错。"""
        pm = PidManager(pid_dir=tmp_dir)
        pm.remove("nonexistent")  # 不应异常

    def test_auto_create_pid_dir(self, tmp_dir):
        """如果 pid_dir 不存在应自动创建。"""
        nested = tmp_dir / "deep" / "nested" / "pid"
        pm = PidManager(pid_dir=nested)
        pm.write("test", 42)
        assert nested.exists()
        assert pm.read("test") == 42

    def test_pid_dir_property(self, tmp_dir):
        """pid_dir 属性返回 PID 目录路径。"""
        pm = PidManager(pid_dir=tmp_dir)
        assert pm.pid_dir == tmp_dir


class TestProcessManager:
    """进程管理测试。"""

    @pytest.fixture
    def pm(self):
        """创建 ProcessManager 实例。"""
        return ProcessManager()

    def test_start_returns_pid(self, pm):
        """start 应返回子进程 PID。"""
        with mock.patch("subprocess.Popen") as mock_popen:
            with mock.patch.object(Path, "exists", return_value=True):
                with mock.patch.object(Path, "mkdir") as _mock_mkdir:
                    with mock.patch("builtins.open", mock.mock_open()):
                        mock_process = mock.MagicMock()
                        mock_process.pid = 12345
                        mock_popen.return_value = mock_process

                        process = pm.start(
                            config_path=Path("/config.yaml"),
                            venv_path=Path("/venv"),
                            log_path=Path("/log/gateway-out.log"),
                        )

                        assert process.pid == 12345
                        mock_popen.assert_called_once()

    def test_start_command_contains_venv_python(self, pm):
        """start 应使用 venv 中的 Python。"""
        with mock.patch("subprocess.Popen") as mock_popen:
            with mock.patch.object(Path, "exists", return_value=True):
                with mock.patch.object(Path, "mkdir") as _mock_mkdir:
                    with mock.patch("builtins.open", mock.mock_open()):
                        mock_process = mock.MagicMock()
                        mock_process.pid = 1
                        mock_popen.return_value = mock_process

                        pm.start(
                            config_path=Path("/etc/nanobee/inst/config.yaml"),
                            venv_path=Path("/opt/.venv"),
                            log_path=Path("/log/gateway-out.log"),
                        )

                        cmd = mock_popen.call_args[0][0]
                        assert str(Path("/opt/.venv/bin/python")) in str(cmd)

    def test_start_command_contains_gateway_subcommand(self, pm):
        """start 命令应包含 nanobee gateway 子命令和 -c 参数。"""
        with mock.patch("subprocess.Popen") as mock_popen:
            with mock.patch.object(Path, "exists", return_value=True):
                with mock.patch.object(Path, "mkdir") as _mock_mkdir:
                    with mock.patch("builtins.open", mock.mock_open()):
                        mock_process = mock.MagicMock()
                        mock_process.pid = 1
                        mock_popen.return_value = mock_process

                        pm.start(
                            config_path=Path("/test/config.yaml"),
                            venv_path=Path("/venv"),
                            log_path=Path("/log/gateway-out.log"),
                        )

                        cmd = mock_popen.call_args[0][0]
                        cmd_str = " ".join(cmd)
                        assert "gateway" in cmd_str
                        assert "-c" in cmd_str
                        assert "/test/config.yaml" in cmd_str

    def test_start_redirects_stdout_stderr_to_log(self, pm):
        """start 应将 stdout/stderr 重定向到日志文件。"""
        with mock.patch("subprocess.Popen") as mock_popen:
            with mock.patch.object(Path, "exists", return_value=True):
                with mock.patch.object(Path, "mkdir") as _mock_mkdir:
                    with mock.patch("builtins.open", mock.mock_open()):
                        mock_process = mock.MagicMock()
                        mock_process.pid = 1
                        mock_popen.return_value = mock_process

                        log_path = Path("/log/gateway-out.log")
                        pm.start(
                            config_path=Path("/config.yaml"),
                            venv_path=Path("/venv"),
                            log_path=log_path,
                        )

                        kwargs = mock_popen.call_args[1]
                        assert "stdout" in kwargs
                        assert "stderr" in kwargs

    def test_stop_sends_sigterm_then_sigkill(self, pm):
        """stop 应先发 SIGTERM，超时后发 SIGKILL。"""
        with mock.patch("os.kill") as mock_kill:
            with mock.patch.object(pm, "_is_process_running", side_effect=[True, True, True, False]):
                pm.stop(pid=12345, timeout=0.1)

            # 应发 SIGTERM 和 SIGKILL（因为进程存活直到超时）
            signals_sent = [call[0][1] for call in mock_kill.call_args_list]
            assert signal.SIGTERM in signals_sent
            assert signal.SIGKILL in signals_sent

    def test_stop_sigterm_success_no_sigkill(self, pm):
        """如果 SIGTERM 后进程立即退出，不应发 SIGKILL。"""
        with mock.patch("os.kill") as mock_kill:
            with mock.patch.object(pm, "_is_process_running", side_effect=[True, False]):
                pm.stop(pid=12345, timeout=5.0)

            # 只应发 SIGTERM
            signals_sent = [call[0][1] for call in mock_kill.call_args_list]
            assert signals_sent == [signal.SIGTERM]

    def test_stop_process_already_dead(self, pm):
        """如果进程已死，stop 应直接返回。"""
        with mock.patch("os.kill") as mock_kill:
            with mock.patch.object(pm, "_is_process_running", return_value=False):
                pm.stop(pid=12345, timeout=5.0)

            mock_kill.assert_not_called()

    def test_is_running_existing_process(self, pm):
        """存在进程时应返回 True。"""
        with mock.patch("os.kill") as mock_kill:
            assert pm.is_running(12345) is True
            mock_kill.assert_called_once_with(12345, 0)

    def test_is_running_dead_process(self, pm):
        """进程不存在时应返回 False。"""
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            assert pm.is_running(12345) is False

    def test_is_running_permission_denied(self, pm):
        """权限不足时视为进程存在（返回 True）。"""
        with mock.patch("os.kill", side_effect=PermissionError):
            assert pm.is_running(12345) is True


@pytest.mark.asyncio
class TestHealthChecker:
    """健康检查轮询测试。"""

    @pytest.fixture
    def hc(self):
        """创建 HealthChecker 实例。"""
        return HealthChecker()

    def _make_session(self, statuses):
        """创建模拟的 aiohttp ClientSession，返回指定状态码序列。"""
        if not isinstance(statuses, list):
            statuses = [statuses]

        def _make_resp(status):
            resp = mock.MagicMock()
            resp.status = status
            return resp

        cm_list = []
        for s in statuses:
            cm = mock.MagicMock()
            if isinstance(s, int):
                cm.__aenter__ = mock.AsyncMock(return_value=_make_resp(s))
                cm.__aexit__ = mock.AsyncMock(return_value=None)
            else:
                # 异常类型
                cm.__aenter__ = mock.AsyncMock(side_effect=s)
                cm.__aexit__ = mock.AsyncMock(return_value=None)
            cm_list.append(cm)

        session = mock.MagicMock()
        session.get = mock.MagicMock()
        if len(cm_list) == 1:
            session.get.return_value = cm_list[0]
        else:
            session.get.side_effect = cm_list
        # 支持 async with session
        session.__aenter__ = mock.AsyncMock(return_value=session)
        session.__aexit__ = mock.AsyncMock(return_value=None)

        mock_cs = mock.MagicMock(return_value=session)
        return mock_cs

    async def test_poll_success_first_attempt(self, hc):
        """第一次尝试即返回 200 时应立即成功。"""
        mock_cs = self._make_session(200)
        with mock.patch("aiohttp.ClientSession", mock_cs):
            success, elapsed = await hc.poll(port=8080, timeout=5.0, interval=1.0)
            assert success is True
            assert elapsed < 5.0

    async def test_poll_retry_then_success(self, hc):
        """前几次失败后最终成功。"""
        mock_cs = self._make_session([503, 503, 200])
        with mock.patch("aiohttp.ClientSession", mock_cs):
            success, _elapsed = await hc.poll(port=8080, timeout=10.0, interval=0.1)
            assert success is True

    async def test_poll_timeout(self, hc):
        """超时后应返回 False。"""
        mock_cs = self._make_session(503)
        with mock.patch("aiohttp.ClientSession", mock_cs):
            success, elapsed = await hc.poll(port=8080, timeout=0.5, interval=1.0)
            assert success is False
            assert elapsed >= 0.5

    async def test_poll_connection_error(self, hc):
        """连接错误时继续重试而非直接失败。"""
        # 先 2 次连接错误，之后始终 503
        call_count = [0]

        def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                cm = mock.MagicMock()
                cm.__aenter__ = mock.AsyncMock(side_effect=ConnectionRefusedError)
                cm.__aexit__ = mock.AsyncMock(return_value=None)
                return cm
            else:
                resp = mock.MagicMock()
                resp.status = 503
                cm = mock.MagicMock()
                cm.__aenter__ = mock.AsyncMock(return_value=resp)
                cm.__aexit__ = mock.AsyncMock(return_value=None)
                return cm

        session = mock.MagicMock()
        session.get.side_effect = _side_effect
        session.__aenter__ = mock.AsyncMock(return_value=session)
        session.__aexit__ = mock.AsyncMock(return_value=None)
        mock_cs = mock.MagicMock(return_value=session)

        with mock.patch("aiohttp.ClientSession", mock_cs):
            success, _elapsed = await hc.poll(port=8080, timeout=2.0, interval=0.1)
            assert success is False  # 最终返回 503 非 200


class TestInstanceDiscovery:
    """实例发现测试。"""

    @pytest.fixture
    def data_dir(self):
        """创建模拟的 data_dir 结构。"""
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def _create_instance(self, data_dir: Path, name: str, port: int = 8080):
        """在 data_dir 下创建实例目录和 config.yaml。"""
        inst_dir = data_dir / name
        inst_dir.mkdir(parents=True)
        config = {"gateway": {"port": port}}
        import yaml
        with open(inst_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)
        return inst_dir

    def test_discover_single_instance(self, data_dir):
        """发现单个实例。"""
        self._create_instance(data_dir, "my-app", 8080)
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert len(instances) == 1
        assert instances[0].name == "my-app"
        assert instances[0].port == 8080

    def test_discover_multiple_instances(self, data_dir):
        """发现多个实例。"""
        self._create_instance(data_dir, "app-a", 8081)
        self._create_instance(data_dir, "app-b", 8082)
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert len(instances) == 2
        names = {i.name for i in instances}
        assert names == {"app-a", "app-b"}

    def test_discover_nonexistent_dir(self, data_dir):
        """不存在目录返回空列表。"""
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir / "nonexistent")
        assert instances == []

    def test_discover_empty_dir(self, data_dir):
        """空目录返回空列表。"""
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert instances == []

    def test_discover_ignores_non_directories(self, data_dir):
        """忽略非目录文件。"""
        (data_dir / "readme.txt").write_text("hello")
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert instances == []

    def test_discover_ignores_dirs_without_config(self, data_dir):
        """忽略没有 config.yaml 的目录。"""
        (data_dir / "empty").mkdir()
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert instances == []

    def test_discover_default_port_when_missing(self, data_dir):
        """config.yaml 中没有 port 字段时使用默认 8080。"""
        inst_dir = data_dir / "no-port"
        inst_dir.mkdir()
        import yaml
        with open(inst_dir / "config.yaml", "w") as f:
            yaml.dump({"gateway": {}}, f)

        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert instances[0].port == 8080

    def test_discover_instance_has_log_path(self, data_dir):
        """发现实例应有正确的 log_path。"""
        self._create_instance(data_dir, "my-app", 8080)
        discovery = InstanceDiscovery()
        instances = discovery.discover(data_dir)
        assert instances[0].log_path == data_dir / "my-app" / "logs" / "gateway-out.log"

    def test_discover_instance_pid_name_is_stable(self, data_dir):
        """相同配置路径应产生相同 pid_name。"""
        self._create_instance(data_dir, "my-app", 8080)
        discovery = InstanceDiscovery()
        i1 = discovery.discover(data_dir)
        i2 = discovery.discover(data_dir)
        assert i1[0].pid_name == i2[0].pid_name


class TestLogReader:
    """日志读取器测试。"""

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_tail_basic(self, tmp_dir):
        """读取日志文件最后 N 行。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(100)))

        reader = LogReader()
        result = reader.tail(log_path, lines=10)
        lines = result.strip().split("\n")
        assert len(lines) == 10
        assert lines[-1] == "line 99"

    def test_tail_less_lines_than_file(self, tmp_dir):
        """文件行数少于请求行数时返回全部。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("line1\nline2\nline3")

        reader = LogReader()
        result = reader.tail(log_path, lines=50)
        assert result.strip().count("\n") == 2

    def test_tail_empty_file(self, tmp_dir):
        """空文件返回空字符串。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("")

        reader = LogReader()
        result = reader.tail(log_path, lines=10)
        assert result == ""

    def test_tail_nonexistent_file(self, tmp_dir):
        """不存在的文件返回空字符串。"""
        reader = LogReader()
        result = reader.tail(tmp_dir / "nonexistent.log", lines=10)
        assert result == ""

    def test_tail_single_line(self, tmp_dir):
        """单行文件正确读取。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("only line")

        reader = LogReader()
        result = reader.tail(log_path, lines=10)
        assert result.strip() == "only line"

    def test_stream_nonexistent_file(self, tmp_dir):
        """stream 不存在的文件返回 None。"""
        reader = LogReader()
        result = reader.stream(tmp_dir / "nonexistent.log")
        assert result is None

    def test_stream_incremental(self, tmp_dir):
        """stream 增量读取仅返回新增内容。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("line1\nline2\n")

        reader = LogReader()
        # 首次 stream 初始化偏移量到文件末尾
        result = reader.stream(log_path)
        assert result is None  # 无新内容

        # 追加新内容
        with open(log_path, "a") as f:
            f.write("line3\nline4\n")

        result = reader.stream(log_path)
        assert result is not None
        assert "line3" in result
        assert "line1" not in result  # 旧内容不返回

    def test_stream_first_read_no_content(self, tmp_dir):
        """已有内容文件首次 stream 返回 None（偏移量定位到末尾）。"""
        log_path = tmp_dir / "test.log"
        log_path.write_text("old content")

        reader = LogReader()
        result = reader.stream(log_path)
        assert result is None


@pytest.mark.asyncio
class TestGatewayRuntime:
    """GatewayRuntime 编排层测试。"""

    @pytest.fixture
    def runtime(self, tmp_path):
        """创建 GatewayRuntime 实例（全部 mock）。"""
        discovery = mock.MagicMock(spec=InstanceDiscovery)
        process_mgr = mock.MagicMock(spec=ProcessManager)
        pid_mgr = mock.MagicMock(spec=PidManager)
        health_checker = mock.MagicMock(spec=HealthChecker)
        log_reader = mock.MagicMock(spec=LogReader)

        # 默认 pid_dir
        pid_mgr.pid_dir = tmp_path / "pid"

        return GatewayRuntime(
            data_dir=str(tmp_path),
            discovery=discovery,
            process_manager=process_mgr,
            pid_manager=pid_mgr,
            health_checker=health_checker,
            log_reader=log_reader,
            venv_path=tmp_path,
        )

    def _make_instance(self, name="test", port=8080):
        """创建测试用 Instance。"""
        return Instance(
            name=name,
            config_path=Path(f"/nanobee-data/{name}/config.yaml"),
            port=port,
            log_path=Path(f"/nanobee-data/{name}/logs/gateway-out.log"),
            pid_name=f"pid-{name}",
        )

    async def test_start_single_instance(self, runtime):
        """start 单个实例：应启动进程、写入 PID、健康检查。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = None
        runtime._health_checker.poll.return_value = (True, 0.5)

        results = await runtime.start("test")

        assert "test" in results
        assert results["test"] is True
        runtime._process_manager.start.assert_called_once()
        runtime._pid_manager.write.assert_called_once()
        runtime._health_checker.poll.assert_called_once()

    async def test_start_already_running(self, runtime):
        """实例已在运行时跳过启动。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = 12345
        runtime._process_manager.is_running.return_value = True

        results = await runtime.start("test")

        assert results["test"] is True
        runtime._process_manager.start.assert_not_called()

    async def test_stop_single_instance(self, runtime):
        """stop 单个实例：应读取 PID、停止进程、删除 PID。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = 12345
        runtime._process_manager.is_running.return_value = True

        results = await runtime.stop("test")

        assert results["test"] is True
        # _stop_one 通过 run_in_executor 调用 stop，参数为位置传递
        runtime._process_manager.stop.assert_called_once_with(12345, 20.0)
        runtime._pid_manager.remove.assert_called_once_with("pid-test")

    async def test_stop_not_running(self, runtime):
        """实例未运行时 stop 应直接成功。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = None

        results = await runtime.stop("test")

        assert results["test"] is True
        runtime._process_manager.stop.assert_not_called()

    async def test_stop_stale_pid(self, runtime):
        """PID 文件存在但进程已死时应清理并成功。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = 12345
        runtime._process_manager.is_running.return_value = False

        results = await runtime.stop("test")

        assert results["test"] is True
        runtime._pid_manager.remove.assert_called_once_with("pid-test")

    async def test_restart(self, runtime):
        """restart 应先 stop 再 start。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        # stop 时返回 12345（触发 stop），之后返回 None（触发 start）
        runtime._pid_manager.read.side_effect = [12345, None]
        runtime._process_manager.is_running.return_value = True
        runtime._health_checker.poll.return_value = (True, 0.3)

        results = await runtime.restart("test")

        assert results["test"] is True
        runtime._process_manager.stop.assert_called_once_with(12345, 20.0)
        runtime._process_manager.start.assert_called_once()

    async def test_status_with_running_instance(self, runtime):
        """status 应正确报告运行中实例。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = 12345
        runtime._process_manager.is_running.return_value = True

        statuses = await runtime.status()

        assert len(statuses) == 1
        assert statuses[0]["name"] == "test"
        assert statuses[0]["pid"] == 12345
        assert statuses[0]["running"] is True

    async def test_status_stale_pid_cleanup(self, runtime):
        """status 应清理僵尸 PID。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._pid_manager.read.return_value = 12345
        runtime._process_manager.is_running.return_value = False

        statuses = await runtime.status()

        assert len(statuses) == 1
        assert statuses[0]["pid"] is None
        assert statuses[0]["running"] is False
        runtime._pid_manager.remove.assert_called_once_with("pid-test")

    async def test_status_no_instances(self, runtime):
        """无实例时 status 返回空列表。"""
        runtime._discovery.discover.return_value = []
        statuses = await runtime.status()
        assert statuses == []

    async def test_logs_basic(self, runtime):
        """logs 应调用 log_reader.tail。"""
        inst = self._make_instance()
        runtime._discovery.discover.return_value = [inst]
        runtime._log_reader.tail.return_value = "line 1\nline 2"

        result = await runtime.logs("test", lines=10)

        runtime._log_reader.tail.assert_called_once_with(inst.log_path, lines=10)
        assert "line 1" in result


