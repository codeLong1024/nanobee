"""
Hook 优先级调度测试 — TDD 驱动实现 FIP 合规的 Hook 元数据与调度机制。

测试分为三层：
1. 元数据层：HookConfig 数据模型、PluginMetadata 集成
2. 声明层：plugin.toml [hooks] 段解析、NanobeePlugin.hook_config
3. 调度层：AgentLoop._notify_plugins_message_completed 分组与调度
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.plugins.base import NanobeePlugin, PluginMetadata


# =============================================================================
# 测试用插件
# =============================================================================


class _BlockingPlugin(NanobeePlugin):
    """声明 block_next=True 的插件。"""
    name = "blocking_plugin"
    plugin_type = "memory"


class _NonBlockingPlugin(NanobeePlugin):
    """声明 block_next=False 的插件。"""
    name = "non_blocking_plugin"
    plugin_type = "audit"


class _SlowPlugin(NanobeePlugin):
    """模拟耗时的插件，用于验证互不阻塞。"""
    name = "slow_plugin"
    plugin_type = "dream"

    def __init__(self, metadata=None, delay: float = 0.05):
        super().__init__(metadata)
        self._delay = delay
        self.call_count = 0

    async def on_message_completed(self, context, messages):
        self.call_count += 1
        await asyncio.sleep(self._delay)


class _FailingPlugin(NanobeePlugin):
    """模拟出错的插件，用于验证异常隔离。"""
    name = "failing_plugin"
    plugin_type = "audit"

    async def on_message_completed(self, context, messages):
        raise RuntimeError("模拟插件异常")

    @property
    def call_count(self):
        return getattr(self, "_call_count", 0)


class _PriorityPlugin(NanobeePlugin):
    """记录调用时序的插件，验证 priority 排序。"""
    name = "priority_plugin"
    plugin_type = "memory"

    def __init__(self, metadata=None):
        super().__init__(metadata)
        self._on_message_completed = None  # 由测试注入

    async def on_message_completed(self, context, messages):
        if self._on_message_completed:
            await self._on_message_completed(context, messages)


# =============================================================================
# Layer 1: HookConfig 数据模型 + PluginMetadata
# =============================================================================


class TestHookConfigModel:
    """验证 HookConfig 数据模型的定义和默认值。"""

    def test_hook_config_importable(self):
        """HookConfig 可从 plugins.base 导入。"""
        from nanobee.plugins.base import HookConfig
        assert HookConfig is not None

    def test_hook_config_default_values(self):
        """HookConfig 默认值：block_next=False, priority=10。"""
        from nanobee.plugins.base import HookConfig
        cfg = HookConfig()
        assert cfg.block_next is False
        assert cfg.priority == 10
        assert cfg.timeout == 0.0  # 0 表示不设超时

    def test_hook_config_custom_values(self):
        """HookConfig 可自定义所有字段。"""
        from nanobee.plugins.base import HookConfig
        cfg = HookConfig(block_next=True, priority=80, timeout=5.0)
        assert cfg.block_next is True
        assert cfg.priority == 80
        assert cfg.timeout == 5.0

    def test_hook_config_from_dict(self):
        """HookConfig.from_dict 从字典构建。"""
        from nanobee.plugins.base import HookConfig
        cfg = HookConfig.from_dict({"block_next": True, "priority": 100})
        assert cfg.block_next is True
        assert cfg.priority == 100

    def test_hook_config_from_empty_dict(self):
        """from_dict 空字典 → 全部默认值。"""
        from nanobee.plugins.base import HookConfig
        cfg = HookConfig.from_dict({})
        assert cfg.block_next is False
        assert cfg.priority == 10


class TestPluginMetadataHooks:
    """验证 PluginMetadata 存储 hooks 配置。"""

    def test_metadata_default_hooks_empty(self):
        """新 PluginMetadata 默认 hooks 为空字典。"""
        meta = PluginMetadata(name="test", plugin_type="memory")
        assert meta.hooks == {}

    def test_metadata_with_hooks(self):
        """PluginMetadata 可携带 hooks 配置。"""
        from nanobee.plugins.base import HookConfig
        meta = PluginMetadata(
            name="test",
            plugin_type="memory",
            hooks={
                "on_message_completed": HookConfig(block_next=True, priority=80),
            },
        )
        assert "on_message_completed" in meta.hooks
        cfg = meta.hooks["on_message_completed"]
        assert cfg.block_next is True
        assert cfg.priority == 80

    def test_metadata_hooks_from_dict(self):
        """hooks 支持从 dict 创建 HookConfig。"""
        from nanobee.plugins.base import HookConfig
        meta = PluginMetadata(
            name="test",
            plugin_type="memory",
            hooks={
                "on_message_completed": {"block_next": True, "priority": 80},
            },
        )
        cfg = meta.hooks["on_message_completed"]
        # 通过 Pydantic validator 自动转换
        assert isinstance(cfg, HookConfig)
        assert cfg.block_next is True
        assert cfg.priority == 80


# =============================================================================
# Layer 2: PluginDescriptor 解析 [hooks] from plugin.toml
# =============================================================================


class TestPluginDescriptorHooks:
    """验证 PluginDescriptor 正确解析 plugin.toml 中的 [hooks] 段。"""

    def test_descriptor_parses_hooks_section(self, tmp_path):
        """plugin.toml 含 [hooks.on_message_completed] → metadata.hooks 正确填充。"""
        from nanobee.kernel.plugin_manager import PluginDescriptor

        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        toml_path = plugin_dir / "plugin.toml"
        toml_path.write_text("""[plugin]
name = "test_hook_plugin"
version = "1.0.0"
description = "测试 Hook 解析"
type = "memory"

[hooks.on_message_completed]
block_next = true
priority = 80
timeout = 5.0
""", encoding="utf-8")

        desc = PluginDescriptor(toml_path)
        hooks = desc.metadata.hooks
        assert "on_message_completed" in hooks
        cfg = hooks["on_message_completed"]
        assert cfg.block_next is True
        assert cfg.priority == 80
        assert cfg.timeout == 5.0

    def test_descriptor_no_hooks_section(self, tmp_path):
        """plugin.toml 无 [hooks] 段 → metadata.hooks 为空字典。"""
        from nanobee.kernel.plugin_manager import PluginDescriptor

        plugin_dir = tmp_path / "test_no_hooks"
        plugin_dir.mkdir()
        toml_path = plugin_dir / "plugin.toml"
        toml_path.write_text("""[plugin]
name = "no_hook_plugin"
version = "1.0"
type = "tool"
""", encoding="utf-8")

        desc = PluginDescriptor(toml_path)
        assert desc.metadata.hooks == {}

    def test_descriptor_hooks_uses_defaults_for_missing_fields(self, tmp_path):
        """[hooks.on_message_completed] 部分字段缺失 → 使用 HookConfig 默认值。"""
        from nanobee.kernel.plugin_manager import PluginDescriptor

        plugin_dir = tmp_path / "test_partial_hooks"
        plugin_dir.mkdir()
        toml_path = plugin_dir / "plugin.toml"
        toml_path.write_text("""[plugin]
name = "partial_hook"
version = "1.0"
type = "audit"

[hooks.on_message_completed]
# 只声明 block_next，其他用默认值
block_next = false
""", encoding="utf-8")

        desc = PluginDescriptor(toml_path)
        cfg = desc.metadata.hooks["on_message_completed"]
        assert cfg.block_next is False
        assert cfg.priority == 10  # 默认值
        assert cfg.timeout == 0.0  # 默认值

    def test_descriptor_multiple_hook_declarations(self, tmp_path):
        """支持声明多个 Hook 的元数据。"""
        from nanobee.kernel.plugin_manager import PluginDescriptor

        plugin_dir = tmp_path / "test_multi_hooks"
        plugin_dir.mkdir()
        toml_path = plugin_dir / "plugin.toml"
        toml_path.write_text("""[plugin]
name = "multi_hook"
version = "1.0"
type = "memory"

[hooks.on_message_completed]
block_next = true
priority = 80

[hooks.on_pre_invoke]
priority = 100
timeout = 3.0
""", encoding="utf-8")

        desc = PluginDescriptor(toml_path)
        hooks = desc.metadata.hooks
        assert len(hooks) == 2
        assert hooks["on_message_completed"].block_next is True
        assert hooks["on_message_completed"].priority == 80
        assert hooks["on_pre_invoke"].priority == 100
        assert hooks["on_pre_invoke"].timeout == 3.0


# =============================================================================
# Layer 3: NanobeePlugin._hook_config 属性
# =============================================================================


class TestNanobeePluginHookConfig:
    """验证 NanobeePlugin 从 metadata.hooks 中提取 hook_config。"""

    def test_plugin_hook_config_defaults(self):
        """无 metadata.hooks 时 hook_config 返回空字典。"""
        plugin = _NonBlockingPlugin()
        assert plugin.hook_config == {}

    def test_plugin_hook_config_from_metadata(self):
        """metadata.hooks 有值 → hook_config 返回对应配置。"""
        from nanobee.plugins.base import HookConfig
        meta = PluginMetadata(
            name="test",
            plugin_type="memory",
            hooks={
                "on_message_completed": HookConfig(block_next=True, priority=80),
            },
        )
        plugin = _BlockingPlugin(metadata=meta)
        config = plugin.hook_config
        assert "on_message_completed" in config
        assert config["on_message_completed"].block_next is True
        assert config["on_message_completed"].priority == 80

    def test_plugin_hook_config_single_hook_key(self):
        """hook_config 支持按 hook 名查询。"""
        from nanobee.plugins.base import HookConfig
        meta = PluginMetadata(
            name="test",
            plugin_type="memory",
            hooks={
                "on_message_completed": HookConfig(block_next=False, priority=5),
            },
        )
        plugin = _NonBlockingPlugin(metadata=meta)
        cfg = plugin.hook_config.get("on_message_completed")
        assert cfg is not None
        assert cfg.block_next is False
        assert cfg.priority == 5


# =============================================================================
# Layer 4: AgentLoop._notify_plugins_message_completed FIP 调度
# =============================================================================


class _FakeContextManager:
    """模拟 ContextManager，按需返回 FakeUserContext。"""
    def __init__(self):
        self.contexts: dict[str, Any] = {}

    async def get_or_create(self, context_id: str) -> Any:
        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.context_id = context_id
        self.contexts[context_id] = ctx
        return ctx


class _FakePluginManager:
    """模拟 PluginManager，手动注入插件列表。"""
    def __init__(self):
        self._plugins: list[NanobeePlugin] = []

    def get_enabled_plugins(self) -> list[NanobeePlugin]:
        return self._plugins

    def add(self, plugin: NanobeePlugin) -> None:
        self._plugins.append(plugin)


class TestHookSchedulingFIP:
    """验证 FIP 调度逻辑：分组（blocking/non-blocking）、排序（priority）、异常隔离。"""

    @pytest.mark.asyncio
    async def test_non_blocking_plugins_fire_and_forget(self):
        """非阻塞插件走 create_task，互不阻塞。"""
        from nanobee.plugins.base import HookConfig

        plugin1 = _SlowPlugin(delay=0.05)
        plugin1._metadata = PluginMetadata(
            name="slow1", plugin_type="dream",
            hooks={"on_message_completed": HookConfig(block_next=False, priority=10)},
        )
        plugin2 = _SlowPlugin(delay=0.05)
        plugin2._metadata = PluginMetadata(
            name="slow2", plugin_type="dream",
            hooks={"on_message_completed": HookConfig(block_next=False, priority=10)},
        )

        pm = _FakePluginManager()
        pm.add(plugin1)
        pm.add(plugin2)

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = pm
        loop.context_manager = _FakeContextManager()
        loop._pending_blockers = {}

        t0 = asyncio.get_event_loop().time()
        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])
        elapsed = asyncio.get_event_loop().time() - t0

        # 两个非阻塞插件都在各自 task 中运行了（而非顺序等待）
        # 注意：_notify_plugins_message_completed 返回后 task 未必执行完
        # 给一个小窗口等待
        await asyncio.sleep(0.15)
        assert plugin1.call_count == 1
        assert plugin2.call_count == 1

    @pytest.mark.asyncio
    async def test_blocking_plugins_create_task_tracked(self):
        """阻塞插件创建 task 并追踪到 _pending_blockers。"""
        from nanobee.plugins.base import HookConfig

        plugin = _SlowPlugin(delay=0.01)
        plugin._metadata = PluginMetadata(
            name="blocker", plugin_type="memory",
            hooks={"on_message_completed": HookConfig(block_next=True, priority=50)},
        )

        pm = _FakePluginManager()
        pm.add(plugin)

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = pm
        loop.context_manager = _FakeContextManager()
        loop._pending_blockers = {}

        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])

        # 应该创建了追踪 task
        assert "ctx-1" in loop._pending_blockers
        task = loop._pending_blockers["ctx-1"]
        assert isinstance(task, asyncio.Task)

        # 等待 task 完成
        await task
        assert plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_error_isolation_one_failing_does_not_block_others(self):
        """单个插件异常不影响其他插件的 on_message_completed 执行。"""
        from nanobee.plugins.base import HookConfig

        fail_plugin = _FailingPlugin()
        fail_plugin._metadata = PluginMetadata(
            name="failing", plugin_type="audit",
            hooks={"on_message_completed": HookConfig(block_next=False, priority=10)},
        )
        ok_plugin = _SlowPlugin(delay=0.01)
        ok_plugin._metadata = PluginMetadata(
            name="ok", plugin_type="audit",
            hooks={"on_message_completed": HookConfig(block_next=False, priority=10)},
        )

        pm = _FakePluginManager()
        pm.add(fail_plugin)
        pm.add(ok_plugin)

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = pm
        loop.context_manager = _FakeContextManager()
        loop._pending_blockers = {}

        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])
        await asyncio.sleep(0.05)
        # ok 插件应该正常完成
        assert ok_plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_priority_ordering_within_blocking_group(self):
        """阻塞插件组内按 priority 降序执行。"""
        from nanobee.plugins.base import HookConfig

        order: list[str] = []

        p1 = _PriorityPlugin()
        p1._metadata = PluginMetadata(
            name="p_low", plugin_type="memory",
            hooks={"on_message_completed": HookConfig(block_next=True, priority=10)},
        )
        p1._on_message_completed = AsyncMock(side_effect=lambda ctx, msgs: order.append("p_low"))

        p2 = _PriorityPlugin()
        p2._metadata = PluginMetadata(
            name="p_high", plugin_type="memory",
            hooks={"on_message_completed": HookConfig(block_next=True, priority=100)},
        )
        p2._on_message_completed = AsyncMock(side_effect=lambda ctx, msgs: order.append("p_high"))

        pm = _FakePluginManager()
        pm.add(p1)
        pm.add(p2)

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = pm
        loop.context_manager = _FakeContextManager()
        loop._pending_blockers = {}

        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])

        # 等待 blocking group task 完成
        task = loop._pending_blockers["ctx-1"]
        await task
        assert order == ["p_high", "p_low"]

    @pytest.mark.asyncio
    async def test_no_plugins_no_error(self):
        """无插件时 _notify_plugins_message_completed 不报错。"""
        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = None  # 无插件
        loop._pending_blockers = {}

        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])
        assert loop._pending_blockers.get("ctx-1") is None

    @pytest.mark.asyncio
    async def test_context_manager_failure_graceful(self):
        """ContextManager 获取失败时优雅跳过，不抛异常。"""
        from nanobee.plugins.base import HookConfig
        plugin = _SlowPlugin(delay=0.01)
        plugin._metadata = PluginMetadata(
            name="slow", plugin_type="dream",
            hooks={"on_message_completed": HookConfig(block_next=False, priority=10)},
        )

        pm = _FakePluginManager()
        pm.add(plugin)

        # 会抛异常的 ContextManager
        class _FailingCtxMgr:
            async def get_or_create(self, context_id):
                raise RuntimeError("模拟数据库故障")

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop.plugin_manager = pm
        loop.context_manager = _FailingCtxMgr()
        loop._pending_blockers = {}

        # 不应抛异常
        await loop._notify_plugins_message_completed("ctx-1", [{"role": "user", "content": "hi"}])


# =============================================================================
# Layer 5: dispatch() 入口 await _pending_blockers
# =============================================================================


class TestDispatchAwaitsBlockers:
    """验证 dispatch() 在给 ctx_id 加锁前，先等待同一 ctx_id 的上一次 blocking hook task。"""

    @pytest.mark.asyncio
    async def test_dispatch_awaits_pending_blocker_before_lock(self):
        """dispatch 入口等待 _pending_blockers[ctx_id] 完成后再获取锁。"""
        events: list[str] = []

        async def _slow_task():
            events.append("blocker_start")
            await asyncio.sleep(0.02)
            events.append("blocker_done")

        blocker_task = asyncio.create_task(_slow_task())

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop._pending_blockers = {"user-1": blocker_task}
        loop._pending_queues = {}

        # LockManager 的 acquire 返回 async context manager
        class _MockLock:
            async def __aenter__(self):
                events.append("lock_acquired")
            async def __aexit__(self, *args):
                pass

        class _MockLockMgr:
            def acquire(self, key):
                events.append("lock_request")
                return _MockLock()

        loop._lock_manager = _MockLockMgr()
        loop._process_message = AsyncMock(return_value=MagicMock(content="ok", media=[]))

        msg = MagicMock()
        msg.context_id = "user-1"

        await loop.dispatch(msg)

        # blocker_task 应在锁获取之前完成
        blocker_idx = events.index("blocker_done")
        lock_idx = events.index("lock_acquired")
        assert blocker_idx < lock_idx, (
            f"blocker_done (idx={blocker_idx}) 应在 lock_acquired (idx={lock_idx}) 之前"
        )

    @pytest.mark.asyncio
    async def test_dispatch_no_blocker_proceeds_normally(self):
        """无 blocking task 时 dispatch 正常进行。"""
        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop._pending_blockers = {}
        loop._pending_queues = {}

        class _MockLock:
            async def __aenter__(self): pass
            async def __aexit__(self, *args): pass

        class _MockLockMgr:
            def acquire(self, key):
                return _MockLock()

        loop._lock_manager = _MockLockMgr()
        loop._process_message = AsyncMock(return_value=MagicMock(content="ok", media=[]))

        msg = MagicMock()
        msg.context_id = "user-1"
        result = await loop.dispatch(msg)
        assert result is not None

    @pytest.mark.asyncio
    async def test_dispatch_removes_blocker_after_await(self):
        """dispatch 完成后清理 _pending_blockers 中的条目。"""
        async def _done():
            pass
        blocker_task = asyncio.create_task(_done())
        await blocker_task  # 确保已完成

        from nanobee.agent.loop import AgentLoop
        loop = object.__new__(AgentLoop)
        loop._pending_blockers = {"user-1": blocker_task}
        loop._pending_queues = {}

        class _MockLock:
            async def __aenter__(self): pass
            async def __aexit__(self, *args): pass

        class _MockLockMgr:
            def acquire(self, key):
                return _MockLock()

        loop._lock_manager = _MockLockMgr()
        loop._process_message = AsyncMock(return_value=MagicMock(content="ok", media=[]))

        msg = MagicMock()
        msg.context_id = "user-1"
        await loop.dispatch(msg)

        assert "user-1" not in loop._pending_blockers
