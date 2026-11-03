"""v2 结构化进度卡交付实现 —— 卡片会话 + 统一推送泵。

架构收敛（方案评审已批准，P1/P3）：
- **所有权**：会话对象挂在 :class:`CardHandle.state` 上随 turn 流动，不存在
  ``chat_id → session`` 的全局注册表。交错到达的入站消息各自持有自己的会话，
  互不覆盖，内容不会串到他人的卡片上。
- **单一写入泵**：正文帧与工具帧统一经 :class:`CardFramePump` 提交——合并为最新
  快照、按最小间隔推送、终态立即冲刷。调用方（流式回调与工具热路径）不等待
  网络，因此不会因卡片 API 排队而拖慢 agent。
- **失败契约**：:meth:`V2CardSession.fail` 把错误文案追加进正文后终态化，保证
  用户在卡片上能看到失败原因（与 legacy 语义一致）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nanobee.builtin.channel_dingtalk.card_client import DingTalkCardClient
from nanobee.builtin.channel_dingtalk.card_pipeline import (
    CardHandle,
    FinishMode,
)
from nanobee.builtin.channel_dingtalk.rate_limiter import RateLimiter
from nanobee.builtin.channel_dingtalk.v2_card_client import DingTalkV2CardClient
from nanobee.builtin.channel_dingtalk.v2_card_renderer import (
    V2_CARD_TEMPLATE_ID,
    V2CardDraft,
    V2CardFrame,
)
from nanobee.utils.logger import logger

# 错误文案前缀（与 legacy ``fail_card`` 的「处理失败: 」语义对齐）
_FAIL_PREFIX = "⚠️"


class CardFramePump:
    """卡片帧统一推送泵：合并最新快照 + 最小间隔 + 终态立即冲刷。

    机制而非策略：节流窗口由构造参数注入（来自渠道配置
    ``stream_push_min_interval``）；合并策略固定为「只保留最新快照」——每帧都是
    幂等的全量快照，合并天然安全，且天然消解了工具密集时的 API 放大。

    ``submit`` 不等待网络：调用方（流式回调 / 工具执行热路径）提交后立即返回，
    网络 IO 与限流排队都发生在本泵的后台任务里。
    """

    def __init__(self, client: Any, *, interval: float, log: Any = None) -> None:
        """初始化推送泵。

        Args:
            client: :class:`DingTalkV2CardClient`（执行实际帧推送）。
            interval: 两帧之间的最小推送间隔（秒）；0 表示不节流。
            log: 日志器（缺省用模块级 logger）。
        """
        self._client = client
        self._interval = max(0.0, float(interval))
        self._logger = log or logger
        self._pending: V2CardFrame | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._last_push_at = 0.0
        self._consecutive_failures = 0
        self._closed = False

    def submit(self, frame: V2CardFrame) -> None:
        """提交一帧快照（覆盖待发帧并唤醒泵）；不等待网络。"""
        if self._closed:
            return
        self._pending = frame
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def drain(self, frame: V2CardFrame) -> bool:
        """终态冲刷：停止泵并立即推送终态帧（跳过节流）。

        Args:
            frame: 终态帧快照。

        Returns:
            True=终态帧已推送；False=已终态（重复冲刷）或推送失败。
        """
        if self._closed:
            return False
        self._closed = True
        await self._stop()
        return await self._push(frame)

    async def close(self) -> None:
        """停止泵（无终态帧可冲刷时的兜底清理）。"""
        self._closed = True
        await self._stop()

    async def _run(self) -> None:
        """泵循环：等待唤醒 → 取最新快照 → 补足节流间隔 → 推送。"""
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if self._closed:
                    return
                frame = self._pending
                self._pending = None
                if frame is None:
                    continue
                delay = self._interval - (time.monotonic() - self._last_push_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                    # 等待期间可能出现更新的快照：合并后只推最新
                    if self._pending is not None:
                        frame = self._pending
                        self._pending = None
                await self._push(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("[V2CARD] 推送泵异常退出")

    async def _push(self, frame: V2CardFrame) -> bool:
        """串行推送一帧（写入串行化 + 失败不抛出）。

        节流时钟与推送结果解耦：失败同样推进 ``_last_push_at``，保证卡片
        接口持续故障时推送频率仍被最小间隔约束，不会按 delta 速率放大
        请求与异常日志。
        """
        async with self._lock:
            try:
                await self._client.push(frame)
            except (httpx.HTTPError, RuntimeError, OSError):
                self._consecutive_failures += 1
                if self._consecutive_failures == 1:
                    self._logger.exception("[V2CARD] 帧推送失败")
                else:
                    self._logger.warning(
                        "[V2CARD] 帧推送连续失败 {} 次", self._consecutive_failures,
                    )
                self._last_push_at = time.monotonic()
                return False
            self._consecutive_failures = 0
            self._last_push_at = time.monotonic()
            return True

    async def _stop(self) -> None:
        """停止后台泵任务（幂等）。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception("[V2CARD] 推送泵停止异常")


@dataclass
class V2CardSession:
    """单次 turn 的 v2 卡片会话（挂在 ``CardHandle.state`` 上）。

    Attributes:
        card_id: 卡片实例 ID（outTrackId）。
        draft: 渲染草稿（步骤 / 正文状态）。
        pump: 统一推送泵（正文帧与工具帧共用）。
    """

    card_id: str
    draft: V2CardDraft
    pump: CardFramePump

    @property
    def finished(self) -> bool:
        """会话是否已进入终态。"""
        return self.draft.finished

    async def stream_delta(self, delta: str) -> bool:
        """累积正文增量并提交快照（不等待网络）。"""
        if self.draft.finished or not delta:
            return False
        self.draft.append_answer(delta)
        self.pump.submit(self.draft.frame())
        return True

    async def progress(self, tool_events: list[dict[str, Any]]) -> bool:
        """按事件更新步骤行并提交快照（与正文共用同一推送泵）。"""
        if self.draft.finished:
            return False
        changed = False
        for event in tool_events:
            phase = event.get("phase")
            call_id = str(event.get("call_id", ""))
            name = str(event.get("name", ""))
            if not call_id or not name:
                continue
            if phase == "start":
                self.draft.tool_started(call_id, name)
                changed = True
            elif phase in ("end", "error"):
                error = event.get("error") if phase == "error" else None
                self.draft.tool_finished(call_id, name, error=error)
                changed = True
        if not changed:
            return False
        self.pump.submit(self.draft.frame())
        return True

    async def finish(self, *, content: str, mode: FinishMode) -> bool:
        """终态化并冲刷终态帧。

        Args:
            content: 终态文案（append 时追加，complete 时为最终正文）。
            mode: 终态语义。

        Returns:
            True=终态帧已推送；False=会话已终态或推送失败。
        """
        if self.draft.finished:
            return False
        if mode == "append":
            if content:
                self.draft.append_answer(f"\n\n{content}")
        else:
            self.draft.set_answer(content)
        self.draft.finalize()
        return await self.pump.drain(self.draft.frame(finished=True))

    async def fail(self, error_text: str) -> bool:
        """失败终态化：保留半截进度并追加错误文案，保证失败原因可见。

        Args:
            error_text: 失败详情（内核系统通知内容或异常文本）。

        Returns:
            True=终态帧已推送；False=会话已终态或推送失败。
        """
        if self.draft.finished:
            return False
        partial = self.draft.answer.strip()
        text = (
            f"{partial}\n\n---\n{_FAIL_PREFIX} {error_text}"
            if partial
            else f"{_FAIL_PREFIX} {error_text}"
        )
        self.draft.set_answer(text)
        self.draft.finalize(error=error_text)
        return await self.pump.drain(self.draft.frame(finished=True))


class V2CardPipeline:
    """v2 结构化进度卡交付实现（满足 :class:`CardPipeline` 协议）。

    每次 :meth:`open` 产出一个**独立的** :class:`V2CardSession` 并挂在返回的
    handle 上——会话不进入任何全局容器，因此不存在跨 turn 的键冲突。
    """

    # 进度展示由卡片步骤行承载，不启动 DingTalk 表情（声明式标记）
    uses_emoji = False

    def __init__(
        self,
        card_client: DingTalkCardClient,
        *,
        push_interval: float = 1.0,
        template_id: str | None = None,
        agent_name: str = "nanobee",
        model: str = "",
        show_metadata: bool = True,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        """初始化。

        Args:
            card_client: 复用的卡片 HTTP 客户端（token 设施）。
            push_interval: 帧推送最小间隔（秒，来自渠道配置）。
            template_id: v2 模板 ID（缺省用渲染器内置公共模板）。
            agent_name: 状态行展示的 agent 名。
            model: 状态行展示的模型名（空则不展示）。
            show_metadata: 是否展示状态行。
            rate_limiter: 共享 QPS 令牌桶（缺省每卡片自建）。
        """
        self._card_client = card_client
        self._push_interval = push_interval
        self._template_id = template_id
        self._agent_name = agent_name
        self._model = model
        self._show_metadata = show_metadata
        self._rate_limiter = rate_limiter

    async def open(
        self, chat_id: str, *, msg_id: str, robot_code: str, target: dict[str, str],
    ) -> CardHandle | None:
        """创建 v2 卡片并返回携带独立会话的 handle。"""
        draft = V2CardDraft(
            agent_name=self._agent_name,
            model=self._model,
            show_metadata=self._show_metadata,
        )
        client = DingTalkV2CardClient(
            self._card_client,
            robot_code,
            target,
            template_id=self._template_id or V2_CARD_TEMPLATE_ID,
            rate_limiter=self._rate_limiter,
        )
        try:
            card_id = await client.create(draft.create_param_map())
        except (httpx.HTTPError, RuntimeError, OSError):
            logger.exception("[V2CARD] 创建失败 chat={}", chat_id)
            return None
        session = V2CardSession(
            card_id=card_id,
            draft=draft,
            pump=CardFramePump(client, interval=self._push_interval),
        )
        return CardHandle(
            card_id=card_id, chat_id=chat_id, msg_id=msg_id,
            pipeline=self, state=session,
        )

    async def stream_delta(self, handle: CardHandle, delta: str) -> bool:
        """投递流式正文增量。"""
        session: V2CardSession | None = handle.state
        if session is None:
            return False
        return await session.stream_delta(delta)

    async def progress(
        self, handle: CardHandle, tool_events: list[dict[str, Any]],
    ) -> bool:
        """投递工具进度事件。"""
        session: V2CardSession | None = handle.state
        if session is None:
            return False
        return await session.progress(tool_events)

    async def stream_end(self, handle: CardHandle, *, resuming: bool) -> bool:
        """流暂停/结束：v2 卡片正文常显于卡片变量，无需保活动作。

        保留协议方法以满足统一调用形状；``resuming`` 参数在此实现中不参与
        判定（卡片状态不因流暂停而变化）。
        """
        return False

    async def finish(
        self, handle: CardHandle, *, content: str, mode: FinishMode,
    ) -> bool:
        """终态化卡片。"""
        session: V2CardSession | None = handle.state
        if session is None:
            return False
        return await session.finish(content=content, mode=mode)

    async def fail(self, handle: CardHandle, error_text: str) -> bool:
        """失败终态化（错误文案写入正文，保证可见）。"""
        session: V2CardSession | None = handle.state
        if session is None:
            return False
        return await session.fail(error_text)

    def is_streaming(self, handle: CardHandle) -> bool:
        """卡片是否仍处于可终态化的生命周期内。"""
        session: V2CardSession | None = handle.state
        return session is not None and not session.finished

    def fallback_metadata(self, handle: CardHandle) -> dict[str, str]:
        """v2 兜底不携带 legacy 卡片协议键（``_card_id``），仅保留消息定位。

        v2 卡片不经 legacy 终态原语，markdown / 媒体兜底无需卡片 ID。
        """
        return {"msg_id": handle.msg_id} if handle.msg_id else {}


__all__ = ["CardFramePump", "V2CardPipeline", "V2CardSession"]
