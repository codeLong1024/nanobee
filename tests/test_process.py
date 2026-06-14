"""测试进程管理模块（nanobee/kernel/process.py）。"""
from __future__ import annotations

import asyncio
import os
import signal
from unittest.mock import patch

import pytest

from nanobee.kernel.process import run_signal_guard


class TestRunSignalGuard:
    """测试 run_signal_guard 函数。"""

    @pytest.mark.asyncio
    async def test_importable(self) -> None:
        """确认函数可导入且可调用。"""
        assert callable(run_signal_guard)

    @pytest.mark.asyncio
    async def test_signal_triggers_return(self) -> None:
        """确认发送 SIGINT 后守卫函数返回。"""
        task = asyncio.create_task(run_signal_guard())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_double_signal_no_error(self) -> None:
        """确认重复信号不会报错。"""
        task = asyncio.create_task(run_signal_guard())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGINT)  # 第二次应被忽略
        await asyncio.wait_for(task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_sigterm_also_triggers(self) -> None:
        """确认 SIGTERM 同样触发守卫返回。"""
        task = asyncio.create_task(run_signal_guard())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_integration_with_mock_kernel_shutdown(self) -> None:
        """确认传入 kernel 时，收到信号后自动调用 shutdown。"""
        shutdown_called = False

        class MockKernel:
            async def shutdown(self) -> None:
                nonlocal shutdown_called
                shutdown_called = True

        kernel = MockKernel()
        task = asyncio.create_task(run_signal_guard(kernel))
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(task, timeout=5.0)
        assert shutdown_called, "kernel.shutdown() 应被调用"

    @pytest.mark.asyncio
    async def test_signal_handler_registration_fallback(self) -> None:
        """确认 add_signal_handler 失败时优雅降级不崩溃。"""
        with patch.object(
            asyncio.get_running_loop(),
            "add_signal_handler",
            side_effect=NotImplementedError("模拟不支持"),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    run_signal_guard(),
                    timeout=0.2,
                )
