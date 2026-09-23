"""出站契约唯一性测试。

背景：出站信息此前存在两条通路、三个发布者、零 schema 约束——cron 结果与
子代理注入各自手写 payload 字面量，字段漂移不会被类型系统发现（kernel 注入
路径已有 ``response.media`` 却未透传、cron 结果投递丢附件，两者同源）。

本测试锁定的契约（``nanobee.outbound`` 为唯一归属）：

1. 模型唯一：出站模型只有一个 import 入口 ``nanobee.outbound``；此前的 agent /
   channel 侧 re-export 路径已收敛删除（2026-09-16）。
2. 载荷唯一：键集合恒定（channel/chat_id/content/media/metadata），由
   :func:`outbound_payload` 单点构造。
3. 不变量：``media`` 恒为 ``list[str]``（非序列 → 空列表、非字符串项丢弃）；
   ``metadata`` 恒为 dict 且为浅拷贝。
4. 发布原语唯一：``event_bus`` 不可用时静默跳过（对齐既有各调用点守卫语义）；
   发布内容与 :func:`outbound_payload` 严格一致。
5. 发布者一致：三个发布者（cron / kernel 注入 / loop 子代理通知）产出的载荷
   键集合完全相同；cron 成功路径与 kernel 注入透传 media，cron 错误通知不带附件。
   三处发布守卫与消费守卫同源：「正文与附件至少一个非空」——纯附件不得被
   任何一层丢弃（2026-09-16 Phase 2 接线）。
6. 通道消费（2026-09-16 Phase 2 落地）：基类 ``_on_agent_outbound`` 按契约整体
   透传载荷，**含 media**——入口不得丢字段（通道是否投递附件是通道自己的选择）。
   守卫为「正文与附件至少一个非空」：纯附件（正文为空）是合法形态，不得丢弃。
   不带 ``media`` 键的旧式载荷仍按空列表处理（向后兼容）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.events.event_bus import EventBus
from nanobee.outbound import (
    OUTBOUND_EVENT,
    OutboundMessage,
    outbound_payload,
    publish_outbound,
)

# 契约键集合：新增出站字段必须同步更新此断言（正是"只改一处"的守卫）
CONTRACT_KEYS = {"channel", "chat_id", "content", "media", "metadata"}

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 契约 1：模型唯一（只保留一条 import 路径）
# ============================================================


class TestContractIdentity:
    """模型与事件名的唯一性。"""

    def test_model_has_single_import_path(self) -> None:
        """出站模型只有一个 import 入口：``nanobee.outbound``（2026-09-16 收敛）。

        原 ``nanobee.channel.message``（整文件，已删）与 ``nanobee.agent.messages``
        的 re-export 均已移除。此处显式锁定「单一入口」，防止 re-export 路径被重新
        引入——同一模型的多条 import 路径是定义/语义漂移的温床。
        """
        from nanobee.agent import messages as agent_messages

        assert OutboundMessage.__module__ == "nanobee.outbound"
        assert not hasattr(agent_messages, "OutboundMessage")
        assert importlib.util.find_spec("nanobee.channel.message") is None

    def test_event_name_is_single_constant(self) -> None:
        """事件名由契约模块唯一声明。"""
        assert OUTBOUND_EVENT == "agent.outbound"

    def test_contract_module_is_leaf(self) -> None:
        """契约模块是叶子模块：单独 import 不得拉起 agent / channel / events 包。

        任一侧被拉起都意味着未来可能形成导入环（agent 与 channel 都依赖本模块）。
        以子进程隔离验证（同进程内其它测试可能已导入这些包，无法作为证据）。
        """
        code = (
            "import sys; import nanobee.outbound; "
            "bad = [p for p in ('nanobee.agent', 'nanobee.channel', 'nanobee.events') "
            "if p in sys.modules]; "
            "assert not bad, (bad, sorted(sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


# ============================================================
# 契约 2/3：模型语义与载荷不变量
# ============================================================


class TestOutboundMessageModel:
    """统一出站模型字段与默认值。"""

    def test_defaults(self) -> None:
        """仅给路由字段时其余字段取默认值。"""
        msg = OutboundMessage(channel="cli", chat_id="direct")

        assert msg.content == ""
        assert msg.reply_to is None
        assert msg.media == []
        assert msg.metadata == {}

    def test_mutable_defaults_are_isolated(self) -> None:
        """media / metadata 默认值不共享同一对象。"""
        first = OutboundMessage(channel="cli", chat_id="a")
        second = OutboundMessage(channel="cli", chat_id="b")

        first.media.append("/tmp/x.pdf")
        first.metadata["k"] = "v"

        assert second.media == []
        assert second.metadata == {}

    def test_positional_construction_preserved(self) -> None:
        """位置参数构造保持兼容（channel, chat_id, content）。"""
        msg = OutboundMessage("cli", "direct", "hi", None, ["/tmp/a.md"], {"s": 1})

        assert (msg.channel, msg.chat_id, msg.content) == ("cli", "direct", "hi")
        assert msg.media == ["/tmp/a.md"]
        assert msg.metadata == {"s": 1}


class TestOutboundPayload:
    """载荷构造（唯一构造点）与不变量。"""

    def test_key_set_is_frozen(self) -> None:
        """载荷键集合恒定。"""
        payload = outbound_payload(OutboundMessage(channel="cli", chat_id="direct"))

        assert set(payload) == CONTRACT_KEYS

    def test_routing_and_content_passthrough(self) -> None:
        """路由字段与正文原样透传（不做任何改写）。"""
        msg = OutboundMessage(
            channel="channel_dingtalk", chat_id="u1", content="周报已生成",
            reply_to="m1", metadata={"severity": "info"},
        )

        payload = outbound_payload(msg)

        assert payload["channel"] == "channel_dingtalk"
        assert payload["chat_id"] == "u1"
        assert payload["content"] == "周报已生成"
        assert payload["metadata"] == {"severity": "info"}

    @pytest.mark.parametrize(
        ("media", "expected"),
        [
            (None, []),
            ([], []),
            (["/tmp/a.pdf"], ["/tmp/a.pdf"]),
            (("/tmp/a.pdf", "/tmp/b.png"), ["/tmp/a.pdf", "/tmp/b.png"]),
            ([1, "/tmp/ok.pdf", None], ["/tmp/ok.pdf"]),
            ("/tmp/a.pdf", []),
            (object(), []),
        ],
    )
    def test_media_normalization(self, media: object, expected: list[str]) -> None:
        """media 归一化为 list[str]：非序列 → 空列表，非字符串项丢弃。"""
        msg = OutboundMessage(channel="cli", chat_id="direct")
        msg.media = media  # type: ignore[assignment]

        assert outbound_payload(msg)["media"] == expected

    def test_media_normalization_tolerates_mock(self) -> None:
        """鸭子类型 / Mock 响应不抛异常（发布者无需类型防御）。"""
        msg = OutboundMessage(channel="cli", chat_id="direct")
        msg.media = MagicMock()  # type: ignore[assignment]

        assert outbound_payload(msg)["media"] == []

    def test_metadata_is_shallow_copy(self) -> None:
        """metadata 浅拷贝：调用方后续改动不影响已构造的载荷。"""
        shared = {"severity": "info"}
        msg = OutboundMessage(channel="cli", chat_id="direct", metadata=shared)

        payload = outbound_payload(msg)
        shared["severity"] = "error"

        assert payload["metadata"] == {"severity": "info"}

    @pytest.mark.parametrize("bad_metadata", [None, MagicMock()])
    def test_metadata_non_dict_falls_back_to_empty(self, bad_metadata: object) -> None:
        """metadata 非 dict 时降级为空字典（不抛异常）。"""
        msg = OutboundMessage(channel="cli", chat_id="direct")
        msg.metadata = bad_metadata  # type: ignore[assignment]

        assert outbound_payload(msg)["metadata"] == {}


# ============================================================
# 契约 4：发布原语唯一
# ============================================================


class TestPublishPrimitive:
    """publish_outbound 是唯一发布出口。"""

    @pytest.mark.asyncio
    async def test_none_bus_is_silent_noop(self) -> None:
        """event_bus 不可用时静默跳过（不抛异常）。"""
        await publish_outbound(None, OutboundMessage(channel="cli", chat_id="direct"))

    @pytest.mark.asyncio
    async def test_published_payload_matches_constructor(self) -> None:
        """发布内容与 outbound_payload 严格一致（载荷只在一处构造）。"""
        bus = EventBus()
        published: list[dict] = []

        async def spy(data):
            published.append(data)

        bus.subscribe(OUTBOUND_EVENT, spy)
        msg = OutboundMessage(
            channel="cli", chat_id="direct", content="done",
            media=["/tmp/report.md"], metadata={"notification_type": "system"},
        )

        await publish_outbound(bus, msg)

        assert published == [outbound_payload(msg)]
        assert set(published[0]) == CONTRACT_KEYS

    @pytest.mark.asyncio
    async def test_publish_exception_propagates(self) -> None:
        """发布异常不被吞掉（由调用方决定降级策略，如 cron 区分投递失败）。"""
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("publish fail"))

        with pytest.raises(RuntimeError, match="publish fail"):
            await publish_outbound(bus, OutboundMessage(channel="cli", chat_id="direct"))


# ============================================================
# 契约 5：三个发布者产出同一契约载荷
# ============================================================


def _make_cron_plugin(tmp_path: Path, handle_message_return: object):
    """构造带可用 kernel/agent_loop/event_bus 的 cron 插件实例。"""
    from nanobee.builtin.tool_cron.plugin import ToolCronPlugin
    from nanobee.plugins.base import PluginMetadata

    plugin = ToolCronPlugin(PluginMetadata(name="tool_cron", plugin_type="tool"))
    kernel = MagicMock()
    kernel.data_dir = str(tmp_path)
    kernel.agent_loop = MagicMock()
    kernel.event_bus = AsyncMock()
    kernel.agent_loop.event_bus = AsyncMock()
    kernel.handle_message = AsyncMock(return_value=handle_message_return)
    plugin.initialize(kernel)
    plugin._default_timezone = "UTC"
    return plugin


def _make_cron_job():
    """构造带有效投递目标的 cron 任务。"""
    from nanobee.builtin.tool_cron.types import CronJob, CronPayload, CronSchedule

    return CronJob(
        id="job_media",
        name="weekly-report",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            message="生成周报", channel="dingtalk", to="user_a", user_id="user_a",
        ),
    )


class TestPublishersShareContract:
    """三个发布者（cron / kernel 注入 / loop 通知）产出同一契约载荷。"""

    @pytest.mark.asyncio
    async def test_all_publishers_emit_identical_key_set(self, tmp_path: Path) -> None:
        """载荷键集合在三个发布者之间完全一致（杜绝某发布者漏字段）。"""
        from nanobee.agent.loop import AgentLoop
        from nanobee.agent.messages import InboundMessage
        from nanobee.kernel.kernel import NanobeeKernel

        # --- 发布者 1：cron 成功路径（带附件） ---
        response = OutboundMessage(
            channel="dingtalk", chat_id="user_a", content="周报已生成",
            media=["/tmp/report.md"],
        )
        cron_plugin = _make_cron_plugin(tmp_path, handle_message_return=response)
        await cron_plugin._on_job_execute(_make_cron_job())
        cron_payload = cron_plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]

        # --- 发布者 2：kernel 注入结果（带附件） ---
        kernel_bus = EventBus()
        kernel_captured: list[dict] = []

        async def kernel_spy(data):
            kernel_captured.append(data)

        kernel_bus.subscribe(OUTBOUND_EVENT, kernel_spy)
        kernel = NanobeeKernel.__new__(NanobeeKernel)
        kernel.event_bus = kernel_bus
        kernel.handle_message = AsyncMock(return_value=OutboundMessage(
            channel="cli", chat_id="direct", content="subagent done",
            media=["/tmp/from_subagent.png"],
        ))
        await kernel._handle_injected_message(InboundMessage(
            channel="cli", sender_id="u", chat_id="direct", content="",
        ))

        # --- 发布者 3：loop 子代理启动通知（无附件源） ---
        loop_bus = EventBus()
        loop_captured: list[dict] = []

        async def loop_spy(data):
            loop_captured.append(data)

        loop_bus.subscribe(OUTBOUND_EVENT, loop_spy)
        loop_stub = SimpleNamespace(event_bus=loop_bus)
        await AgentLoop._on_subagent_spawned(loop_stub, {
            "channel": "cli", "chat_id": "direct",
            "label": "report", "task_id": "t-1", "task": "生成周报",
        })

        payloads = [cron_payload, kernel_captured[0], loop_captured[0]]
        for payload in payloads:
            assert set(payload) == CONTRACT_KEYS, payload

    @pytest.mark.asyncio
    async def test_cron_success_carries_media(self, tmp_path: Path) -> None:
        """cron 成功路径透传附件（本次契约落地的核心用例）。"""
        response = OutboundMessage(
            channel="dingtalk", chat_id="user_a", content="周报已生成",
            media=["/tmp/report.md", "https://example.com/x.pdf"],
        )
        plugin = _make_cron_plugin(tmp_path, handle_message_return=response)

        await plugin._on_job_execute(_make_cron_job())

        payload = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert payload["media"] == ["/tmp/report.md", "https://example.com/x.pdf"]

    @pytest.mark.asyncio
    async def test_cron_result_without_media_yields_empty_list(self, tmp_path: Path) -> None:
        """结果无 media 属性 / 为空时载荷 media 为空列表（行为与改造前一致）。"""
        response = MagicMock()
        response.content = "任务完成"
        response.metadata = {}
        response.media = []
        plugin = _make_cron_plugin(tmp_path, handle_message_return=response)

        await plugin._on_job_execute(_make_cron_job())

        payload = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert payload["media"] == []

    @pytest.mark.asyncio
    async def test_cron_error_notice_carries_no_media(self, tmp_path: Path) -> None:
        """错误通知不带附件（正文即错误文案，附件只会造成误导）。"""
        response = MagicMock()
        response.content = "抱歉，处理消息时发生内部错误。"
        response.media = ["/tmp/report.md"]
        response.metadata = {
            "notification_type": "system",
            "notification_kind": "turn_internal_error",
            "severity": "error",
            "error_detail": "RuntimeError: LLM 调用失败",
        }
        plugin = _make_cron_plugin(tmp_path, handle_message_return=response)

        from nanobee.builtin.tool_cron.types import CronJobError

        with pytest.raises(CronJobError):
            await plugin._on_job_execute(_make_cron_job())

        payload = plugin.kernel.agent_loop.event_bus.publish.await_args.args[1]
        assert payload["media"] == []
        assert payload["metadata"]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_injected_response_carries_media(self) -> None:
        """kernel 注入路径透传 response.media（此前有数据却未透传的缺陷）。"""
        bus = EventBus()
        captured: list[dict] = []

        async def spy(data):
            captured.append(data)

        bus.subscribe(OUTBOUND_EVENT, spy)

        from nanobee.agent.messages import InboundMessage
        from nanobee.kernel.kernel import NanobeeKernel

        kernel = NanobeeKernel.__new__(NanobeeKernel)
        kernel.event_bus = bus
        kernel.handle_message = AsyncMock(return_value=OutboundMessage(
            channel="cli", chat_id="direct", content="子代理结果",
            media=["/tmp/from_subagent.png"],
        ))

        await kernel._handle_injected_message(InboundMessage(
            channel="cli", sender_id="u", chat_id="direct", content="",
        ))

        assert len(captured) == 1
        assert captured[0]["media"] == ["/tmp/from_subagent.png"]
        assert set(captured[0]) == CONTRACT_KEYS

    @pytest.mark.asyncio
    async def test_injected_pure_attachment_is_published(self) -> None:
        """纯附件注入结果（正文为空 + media）不得被发布侧守卫丢弃。"""
        bus = EventBus()
        captured: list[dict] = []

        async def spy(data):
            captured.append(data)

        bus.subscribe(OUTBOUND_EVENT, spy)

        from nanobee.agent.messages import InboundMessage
        from nanobee.kernel.kernel import NanobeeKernel

        kernel = NanobeeKernel.__new__(NanobeeKernel)
        kernel.event_bus = bus
        kernel.handle_message = AsyncMock(return_value=OutboundMessage(
            channel="cli", chat_id="direct", content="",
            media=["/tmp/周报.md"],
        ))

        await kernel._handle_injected_message(InboundMessage(
            channel="cli", sender_id="u", chat_id="direct", content="",
        ))

        assert len(captured) == 1
        assert captured[0]["content"] == ""
        assert captured[0]["media"] == ["/tmp/周报.md"]

    @pytest.mark.asyncio
    async def test_injected_empty_response_is_not_published(self) -> None:
        """正文与附件都为空 → 不发布（放开守卫≠全放行）。"""
        bus = EventBus()
        captured: list[dict] = []

        async def spy(data):
            captured.append(data)

        bus.subscribe(OUTBOUND_EVENT, spy)

        from nanobee.agent.messages import InboundMessage
        from nanobee.kernel.kernel import NanobeeKernel

        kernel = NanobeeKernel.__new__(NanobeeKernel)
        kernel.event_bus = bus
        kernel.handle_message = AsyncMock(return_value=OutboundMessage(
            channel="cli", chat_id="direct", content="", media=[],
        ))

        await kernel._handle_injected_message(InboundMessage(
            channel="cli", sender_id="u", chat_id="direct", content="",
        ))

        assert captured == []

    @pytest.mark.asyncio
    async def test_subagent_notice_has_empty_media_and_system_metadata(self) -> None:
        """子代理启动通知：无附件源 → media 空列表，metadata 三键保持原样。"""
        from nanobee.agent.loop import AgentLoop

        bus = EventBus()
        captured: list[dict] = []

        async def spy(data):
            captured.append(data)

        bus.subscribe(OUTBOUND_EVENT, spy)
        loop_stub = SimpleNamespace(event_bus=bus)

        await AgentLoop._on_subagent_spawned(loop_stub, {
            "channel": "cli", "chat_id": "direct",
            "label": "report", "task_id": "t-1", "task": "生成周报",
        })

        payload = captured[0]
        assert payload["media"] == []
        assert payload["metadata"]["notification_type"] == "system"
        assert payload["metadata"]["notification_kind"] == "subagent_spawned"
        assert payload["metadata"]["severity"] == "info"


# ============================================================
# 契约 6：通道消费（基类透传 media + 纯附件形态）
# ============================================================


def _make_stub_channel():
    """构造一个记录 send 调用的最小通道实例。"""
    from nanobee.channel.base import ChannelPlugin
    from nanobee.plugins.base import PluginMetadata

    class _RecordingChannel(ChannelPlugin):
        def __init__(self) -> None:
            super().__init__(PluginMetadata(name="stub_channel", plugin_type="channel"))
            self.sent: list[tuple[OutboundMessage, str]] = []

        async def send(self, message: OutboundMessage, context_id: str = "default") -> None:
            self.sent.append((message, context_id))

    return _RecordingChannel()


class TestConsumerBackwardCompat:
    """通道基类对契约载荷的消费行为。"""

    @pytest.mark.asyncio
    async def test_legacy_payload_without_media_key_still_consumed(self) -> None:
        """旧式载荷（无 media 键）仍可被消费，媒体按缺省空列表处理。"""
        channel = _make_stub_channel()

        await channel._on_agent_outbound({
            "channel": "stub_channel",
            "chat_id": "conv-1",
            "content": "旧式载荷",
            "metadata": {},
        })

        assert len(channel.sent) == 1
        message, context_id = channel.sent[0]
        assert message.content == "旧式载荷"
        assert message.media == []
        assert context_id == "conv-1"

    @pytest.mark.asyncio
    async def test_media_key_forwarded_by_channel_base(self) -> None:
        """载荷带 media 时基类**原样透传**（Phase 2 接线；入口不丢字段）。"""
        channel = _make_stub_channel()

        await channel._on_agent_outbound({
            "channel": "stub_channel",
            "chat_id": "conv-1",
            "content": "正文",
            "media": ["/tmp/report.md", "https://example.com/r.md"],
            "metadata": {},
        })

        assert len(channel.sent) == 1
        message, _ = channel.sent[0]
        assert message.content == "正文"
        assert message.media == ["/tmp/report.md", "https://example.com/r.md"]

    @pytest.mark.asyncio
    async def test_pure_attachment_event_is_delivered(self) -> None:
        """纯附件（正文为空 + media 非空）必须投递——周报形态，不得被守卫吞掉。"""
        channel = _make_stub_channel()

        await channel._on_agent_outbound({
            "channel": "stub_channel",
            "chat_id": "conv-1",
            "content": "",
            "media": ["/tmp/周报.md"],
            "metadata": {},
        })

        assert len(channel.sent) == 1
        message, context_id = channel.sent[0]
        assert message.content == ""
        assert message.media == ["/tmp/周报.md"]
        assert context_id == "conv-1"

    @pytest.mark.asyncio
    async def test_empty_content_and_no_media_is_skipped(self) -> None:
        """正文与附件都为空才跳过（放开集合守卫不等于全放行）。"""
        channel = _make_stub_channel()

        await channel._on_agent_outbound({
            "channel": "stub_channel",
            "chat_id": "conv-1",
            "content": "",
            "media": [],
            "metadata": {},
        })

        assert channel.sent == []
