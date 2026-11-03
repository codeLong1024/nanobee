"""卡片交付契约 —— 一次 turn 的卡片生命周期（开卡 → 流式 → 进度 → 终态/失败）。

架构收敛（方案评审已批准，P1/P2）：
- **所有权**：:class:`CardHandle` 是 turn 级所有权载体，随回调闭包流动，不做
  全局键查找。每条入站消息各自持有自己的 handle，彼此不可见；「后一条消息
  覆盖前一条消息的卡片」在结构上不可能发生。
- **单一出口**：出口分支语义（系统通知 / 正常完成 / max_iterations / 失败）
  只在 ``channel._deliver_response`` 判定一次；各实现只负责把语义映射到自己
  的卡片原语，避免同一语义两处实现造成的语义漂移。
- **失败契约**：:meth:`CardPipeline.fail` 必须让错误文案在卡片上可见（legacy
  与 v2 一致）。这是协议级约束，任一实现遗漏都会被协议一致性测试暴露。

本模块为 legacy（单 markdown 正文 AI 卡片）实现；v2 结构化进度卡实现在
``v2_card_session`` 中，两者共同满足 :class:`CardPipeline` 协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol, runtime_checkable

from nanobee.builtin.channel_dingtalk.card_manager import CardManager

__all__ = ["CardHandle", "CardPipeline", "FinishMode", "LegacyCardPipeline"]

# 终态语义：complete=正常完成（content 为最终正文）；append=在既有内容后追加通知
FinishMode = Literal["complete", "append"]


@dataclass(frozen=True)
class CardHandle:
    """一次 turn 的卡片所有权句柄。

    Attributes:
        card_id: 卡片实例 ID（legacy=``card_instance_id``；v2=``outTrackId``）。
        chat_id: 会话 ID（markdown 兜底与媒体投递使用）。
        msg_id: 消息 ID（legacy 终态通知与表情定位使用）。
        pipeline: 该卡片所属的交付实现。
        state: 不透明状态，由各 pipeline 自行解释（v2 存放会话对象）。
    """

    card_id: str
    chat_id: str
    msg_id: str
    pipeline: CardPipeline
    state: Any = None


@runtime_checkable
class CardPipeline(Protocol):
    """卡片交付契约：一次 turn 的开卡 / 流式 / 进度 / 流结束 / 终态 / 失败。

    布尔返回值语义为「卡片侧是否已消费该事件（或已完成该语义）」；返回
    ``False`` 时由调用方按各自策略兜底（通常回落 markdown）。
    """

    async def open(
        self, chat_id: str, *, msg_id: str, robot_code: str, target: dict[str, str],
    ) -> CardHandle | None:
        """创建并投放一张卡片，返回所有权句柄；失败返回 ``None``。"""

    async def stream_delta(self, handle: CardHandle, delta: str) -> bool:
        """投递流式正文增量。"""

    async def progress(
        self, handle: CardHandle, tool_events: list[dict[str, Any]],
    ) -> bool:
        """投递工具进度事件（start/end/error）。"""

    async def stream_end(self, handle: CardHandle, *, resuming: bool) -> bool:
        """投递流暂停/结束信号（``resuming`` 表示后续还有工具调用）。"""

    async def finish(
        self, handle: CardHandle, *, content: str, mode: FinishMode,
    ) -> bool:
        """终态化卡片。

        Args:
            content: 终态文案（``append`` 时追加到既有内容，``complete`` 时为最终正文）。
            mode: 终态语义。
        """

    async def fail(self, handle: CardHandle, error_text: str) -> bool:
        """失败终态化，并保证错误文案在卡片上可见。"""

    def fallback_metadata(self, handle: CardHandle) -> dict[str, str]:
        """声明本实现兜底（markdown / 媒体）出站所需的 metadata。

        卡片协议键（如 legacy 的 ``_card_id``）由实现各自声明，通道层不得
        代填——否则 v2 的 outTrackId 会被 sender 当 legacy 卡号执行 legacy
        终态原语，造成跨协议串写。
        """

    def is_streaming(self, handle: CardHandle) -> bool:
        """该卡片是否处于可终态化的流式生命周期内。"""

    uses_emoji: bool
    """是否由本实现启动 DingTalk 表情反馈（声明式标记，调用方只读标记）。"""


class LegacyCardPipeline:
    """legacy AI 卡片（单 markdown 正文）交付实现。

    原 ``channel._deliver_*`` 分支中的卡片侧逻辑整体迁入本类，行为不变：错误
    完结的「半截进度 + 失败提示」拼装、max_iterations / 系统通知的终态追加、
    工具开始时的表情反馈。
    """

    uses_emoji = True

    def __init__(
        self, *, sender: Any, card_manager: Any, channel_name: str, logger: Any,
    ) -> None:
        """初始化。

        Args:
            sender: :class:`DingTalkSender`（流式、markdown 与媒体投递）。
            card_manager: :class:`CardManager`（卡片原语）。
            channel_name: 通道名（sender 按此路由出站消息）。
            logger: 日志器。
        """
        self._sender = sender
        self._card_manager = card_manager
        self._channel_name = channel_name
        self._logger = logger

    async def open(
        self, chat_id: str, *, msg_id: str, robot_code: str, target: dict[str, str],
    ) -> CardHandle | None:
        """创建 legacy AI 卡片并以「思考中...」开启流式。"""
        if self._sender is None or self._card_manager is None:
            return None
        try:
            card_id = await self._card_manager.create_card(
                card_instance_id=CardManager.generate_track_id(),
                robot_code=robot_code,
                target=target,
            )
            await self._card_manager.start_streaming(card_id, "思考中...")
        except Exception as e:  # noqa: BLE001 - 卡片创建失败必须降级而非中断 turn
            # 预格式化单参数：loguru 无 %-占位，避免与 stdlib logging 规则误判
            self._logger.warning(f"[CARD] AI Card setup failed: {e}")
            return None
        return CardHandle(
            card_id=card_id, chat_id=chat_id, msg_id=msg_id, pipeline=self,
        )

    async def stream_delta(self, handle: CardHandle, delta: str) -> bool:
        """流式增量交给 sender（其内部按 card 节流累积并推送）。"""
        if self._sender is None or not delta:
            return False
        await self._sender.send(SimpleNamespace(
            channel=self._channel_name, chat_id=handle.chat_id,
            content=delta, media=(),
            metadata={
                "_stream_delta": True,
                "_card_id": handle.card_id,
                "msg_id": handle.msg_id,
            },
        ))
        return True

    async def progress(
        self, handle: CardHandle, tool_events: list[dict[str, Any]],
    ) -> bool:
        """工具**开始**时更新表情为该工具名。

        只响应 ``phase == "start"``：``tool_hint`` 语义已收敛为「新工具开始」，
        end/error 不得复用（否则会把表情回设为已结束的工具名，并多消耗一次
        表情 API 调用）。
        """
        if self._sender is None:
            return False
        for event in tool_events:
            if event.get("phase") != "start":
                continue
            tool_name = event.get("name", "")
            if tool_name:
                # 直接以工具名作为原始 emotion 名称（不走标准 state 查找）
                await self._sender._trigger_emotion(
                    handle.msg_id or handle.chat_id, f"🔧{tool_name}",
                )
                return True
        return False

    async def stream_end(self, handle: CardHandle, *, resuming: bool) -> bool:
        """通知 sender 流暂停（工具调用）或流结束。"""
        if self._sender is None:
            return False
        await self._sender.send(SimpleNamespace(
            channel=self._channel_name, chat_id=handle.chat_id,
            content="", media=(),
            metadata={
                "_stream_end": True,
                "_resuming": resuming,
                "_card_id": handle.card_id,
                "msg_id": handle.msg_id,
            },
        ))
        return True

    async def finish(
        self, handle: CardHandle, *, content: str, mode: FinishMode,
    ) -> bool:
        """终态化 legacy 卡片。

        ``complete``：正文已由流式路径逐帧送达，无需再写（仅投递媒体）。
        ``append``：在既有正文后追加通知（max_iterations / 系统通知）。
        """
        if self._sender is None:
            return False
        # 未经流式处理的卡片终态化会得到空卡，交由调用方 markdown 兜底
        if not self._sender.is_card_handled_by_streaming(handle.card_id):
            return False
        if mode == "append" and content:
            await self._sender.finalize_card_with_notification(
                handle.card_id, handle.msg_id or handle.chat_id, content,
            )
        return True

    async def fail(self, handle: CardHandle, error_text: str) -> bool:
        """错误完结：保留半截流式进度并追加失败提示，保证错误文案可见。"""
        if self._card_manager is None:
            return False
        text = error_text
        if self._sender is not None:
            partial = self._sender.take_stream_buffer(handle.card_id)
            if partial.strip():
                text = f"{partial.strip()}\n\n---\n⚠️ {error_text}"
        return await self._card_manager.fail_card(handle.card_id, text)

    def is_streaming(self, handle: CardHandle) -> bool:
        """legacy 卡片是否已通过流式路径处理（sender 持有的状态）。"""
        if self._sender is None:
            return False
        return bool(self._sender.is_card_handled_by_streaming(handle.card_id))

    def fallback_metadata(self, handle: CardHandle) -> dict[str, str]:
        """legacy 兜底携带卡片 ID（触发 sender 卡片终态原语）与消息定位。"""
        metadata: dict[str, str] = {}
        if handle.card_id:
            metadata["_card_id"] = handle.card_id
        if handle.msg_id:
            metadata["msg_id"] = handle.msg_id
        return metadata
