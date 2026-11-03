"""钉钉 v2 结构化 AI 卡片（JSON 构建卡片）协议验证 demo。

目标：在真实钉钉会话里验证「一张卡片同时承载工具调用步骤 + 流式正文 + 状态行」，
即 Codex CLI 那种"边跑边看得见进度"的聊天窗口效果。本脚本**不改动框架代码**，
只做协议验证，为后续合入 ``channel_dingtalk`` 提供依据。

协议依据（钉钉官方连接器 dingtalk-openclaw-connector v0.8.26 的 v2 卡片实现，
非本项目的推测）：

- 模板：``675cde2f-f526-40cb-b828-f5b2b57b8b77.schema``（官方公共模板）
- 卡片变量：``blockList`` / ``content`` / ``copy_content`` / ``statusLine`` /
  ``flowStatus`` / ``hasAction``
- block 类型：``{"type": 0, "markdown": 正文}``、``{"type": 2, "markdown": 工具状态}``、
  ``{"type": 3, "mediaId": 图片}``
- flowStatus：``"1"`` 初始、``"2"`` 生成中、``"3"`` 已完成
- 每次更新两步：先 PUT ``/v1.0/card/streaming``（key=content，驱动打字机），
  再 PUT ``/v1.0/card/instances``（提交 blockList + flowStatus）

用法::

    export DINGTALK_CLIENT_ID=xxx
    export DINGTALK_CLIENT_SECRET=xxx
    export DINGTALK_ROBOT_CODE=xxx
    export DINGTALK_OPEN_CONVERSATION_ID=cidXXXX   # 群聊
    # 或 export DINGTALK_USER_ID=userXXXX          # 单聊（二选一）

    python examples/dingtalk_card_v2_demo.py              # 真实发送
    python examples/dingtalk_card_v2_demo.py --dry-run    # 只打印请求体，不联网
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from nanobee.builtin.channel_dingtalk.token import TokenManager
from nanobee.utils.logger import logger

API_BASE = "https://api.dingtalk.com/v1.0"

# 官方公共 v2 模板。若当前应用无权使用，需把官方仓库
# docs/assets/card-template-v2.json 导入卡片平台后用 --template-id 指定自有模板。
V2_TEMPLATE_ID = "675cde2f-f526-40cb-b828-f5b2b57b8b77.schema"

# flowStatus 取值：1=初始 / 2=生成中 / 3=已完成
FLOW_INIT = "1"
FLOW_INPUTING = "2"
FLOW_FINISHED = "3"

# 单个 block 的字节上限（官方实现按 UTF-8 计数，留 256 字节余量）
BLOCK_BYTES = 6000

# 演示用最小推送间隔，避免触发钉钉卡片 QPS 限制（框架化时应由配置暴露）
DEFAULT_PUSH_INTERVAL = 0.35

# 工具步骤状态文案（对应 progress_events 的 phase：start / end / error）
STATE_LABELS = {"running": "执行中", "completed": "已完成", "failed": "失败"}


@dataclass
class ToolStep:
    """卡片上的一条工具调用步骤。

    Attributes:
        call_id: 工具调用 ID，用于去重更新同一条步骤。
        name: 工具名。
        state: 状态键，取值 ``running`` / ``completed`` / ``failed``。
    """

    call_id: str
    name: str
    state: str


class CardV2Draft:
    """v2 卡片草稿：维护工具步骤与流式正文，产出 blockList 与 statusLine。"""

    def __init__(self, agent_name: str = "nanobee-demo", model: str = "demo-model") -> None:
        self._steps: list[ToolStep] = []
        self._answer = ""
        self._agent_name = agent_name
        self._model = model
        self._started_at = time.time()

    def set_tool(self, call_id: str, name: str, state: str) -> None:
        """新增或更新一条工具步骤。

        Args:
            call_id: 工具调用 ID。
            name: 工具名。
            state: 状态键，取值 ``running`` / ``completed`` / ``failed``。
        """
        for step in self._steps:
            if step.call_id == call_id:
                step.name = name
                step.state = state
                return
        self._steps.append(ToolStep(call_id=call_id, name=name, state=state))

    def append_answer(self, chunk: str) -> None:
        """追加一段流式正文。

        Args:
            chunk: 本帧新增文本。
        """
        self._answer += chunk

    @property
    def answer(self) -> str:
        """当前累积的正文。"""
        return self._answer

    def blocks(self, *, final: bool = False) -> list[dict[str, Any]]:
        """产出 blockList：先工具步骤（type 2），终帧再追加正文（type 0）。

        非终帧的正文只走 ``content`` 变量（打字机通道），不进 blockList，
        否则同一段文字会被渲染两次。

        Args:
            final: 是否为终态帧。
        """
        blocks: list[dict[str, Any]] = [
            {
                "type": 2,
                "markdown": f"{step.name} · {STATE_LABELS.get(step.state, step.state)}",
            }
            for step in self._steps
        ]
        if final and self._answer.strip():
            blocks.extend(
                {"type": 0, "markdown": chunk} for chunk in split_markdown(self._answer.strip())
            )
        return blocks

    def status_line(self, outcome: str = "生成中") -> str:
        """产出状态行：Agent · 模型 · 工具数 · 耗时 · 状态。"""
        elapsed = max(0, int(time.time() - self._started_at))
        return f"{self._agent_name} · {self._model} · 工具 {len(self._steps)} · {elapsed}s · {outcome}"


def split_markdown(text: str) -> list[str]:
    """按单 block 字节上限切分 markdown，尽量在行边界断开。

    Args:
        text: 待切分文本。

    Returns:
        切分后的文本片段列表。
    """
    chunks: list[str] = []
    current = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) > BLOCK_BYTES - 256 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class DingTalkCardV2:
    """v2 结构化卡片的创建、投放与更新。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token_fn: Callable[[], Awaitable[str]],
        robot_code: str,
        target: dict[str, str],
        *,
        template_id: str = V2_TEMPLATE_ID,
        dry_run: bool = False,
        push_interval: float = DEFAULT_PUSH_INTERVAL,
    ) -> None:
        self._http = http
        self._token_fn = token_fn
        self._robot_code = robot_code
        self._target = target
        self._template_id = template_id
        self._dry_run = dry_run
        self._push_interval = push_interval
        self._last_push_at = 0.0
        self._terminal = False
        self.out_track_id = f"card_{int(time.time() * 1000)}_{random.randint(100000, 999999)}"

    async def _headers(self) -> dict[str, str]:
        """构造带 token 的请求头。"""
        token = "DRY_RUN_TOKEN" if self._dry_run else await self._token_fn()
        return {"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}

    async def _request(self, method: str, url: str, body: dict[str, Any]) -> None:
        """发送一次请求；dry-run 模式只打印请求体。"""
        if self._dry_run:
            logger.info("[DRY-RUN] {} {} body={}", method, url, json.dumps(body, ensure_ascii=False)[:1200])
            return
        headers = await self._headers()
        resp = await self._http.request(method, f"{API_BASE}{url}", headers=headers, json=body)
        logger.info("[CARD] {} {} status={} body={}", method, url, resp.status_code, resp.text[:300])
        resp.raise_for_status()

    async def create_and_deliver(self) -> str:
        """创建并投放卡片，返回 outTrackId。"""
        await self._request(
            "POST",
            "/card/instances",
            {
                "cardTemplateId": self._template_id,
                "outTrackId": self.out_track_id,
                "cardData": {
                    "cardParamMap": {
                        "blockList": "[]",
                        "content": "",
                        "copy_content": "",
                        "statusLine": "",
                        "flowStatus": FLOW_INIT,
                        "hasAction": "false",
                    }
                },
                "callbackType": "STREAM",
                "imGroupOpenSpaceModel": {"supportForward": True},
                "imRobotOpenSpaceModel": {"supportForward": True},
            },
        )
        await self._request("POST", "/card/instances/deliver", self._deliver_body())
        return self.out_track_id

    def _deliver_body(self) -> dict[str, Any]:
        """构造投放请求体（群聊/单聊两种 openSpaceId）。"""
        user_id = self._target.get("receiverUserId", "")
        if user_id:
            return {
                "outTrackId": self.out_track_id,
                "userIdType": 1,
                "openSpaceId": f"dtv1.card//IM_ROBOT.{user_id}",
                "imRobotOpenDeliverModel": {
                    "spaceType": "IM_ROBOT",
                    "robotCode": self._robot_code,
                    "extension": {"dynamicSummary": "true"},
                },
            }
        return {
            "outTrackId": self.out_track_id,
            "userIdType": 1,
            "openSpaceId": f"dtv1.card//IM_GROUP.{self._target['openConversationId']}",
            "imGroupOpenDeliverModel": {"robotCode": self._robot_code},
        }

    async def push(self, draft: CardV2Draft, *, finished: bool = False) -> None:
        """推送一次卡片更新。

        两步式：先走 streaming 激活/关闭流式生命周期，再提交 blockList 与 flowStatus。
        终态之后拒绝迟到帧，避免把已完成的卡片改回生成中。

        Args:
            draft: 卡片草稿。
            finished: 是否为终态帧。
        """
        if self._terminal:
            logger.warning("[CARD] 卡片已终态，忽略迟到帧 {}", self.out_track_id)
            return

        wait = self._push_interval - (time.time() - self._last_push_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_push_at = time.time()

        preview = draft.answer
        # 第一步：streaming 通道（key=content，终帧置空以关闭打字机）
        await self._request(
            "PUT",
            "/card/streaming",
            {
                "outTrackId": self.out_track_id,
                "guid": f"{int(time.time() * 1000)}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}",
                "key": "content",
                "content": "" if finished else preview,
                "isFull": True,
                "isFinalize": finished,
                "isError": False,
            },
        )
        # 第二步：提交结构化 blockList 与状态
        await self._request(
            "PUT",
            "/card/instances",
            {
                "outTrackId": self.out_track_id,
                "cardData": {
                    "cardParamMap": {
                        "blockList": json.dumps(draft.blocks(final=finished), ensure_ascii=False),
                        "content": preview,
                        "copy_content": preview if finished else "",
                        "flowStatus": FLOW_FINISHED if finished else FLOW_INPUTING,
                        "hasAction": "false",
                        "statusLine": draft.status_line("已完成" if finished else "生成中"),
                    }
                },
                "cardUpdateOptions": {"updateCardDataByKey": True},
            },
        )
        if finished:
            self._terminal = True


# 演示用的一次"假 agent 运行"：真实接入时由 runner 的 progress 事件驱动
_DEMO_STEPS = [
    ("call_1", "读取文件 report.md"),
    ("call_2", "查询数据库 ebsql"),
    ("call_3", "生成汇总图表"),
]
_DEMO_ANSWER_CHUNKS = [
    "已读取 report.md，共 3 个章节。\n\n",
    "数据库查询返回 128 行记录，按月份聚合。\n\n",
    "汇总结论：\n",
    "- 一季度同比增长 12%\n",
    "- 库存周转天数下降 3 天\n",
]


async def simulate_agent_run(card: DingTalkCardV2, draft: CardV2Draft, step_delay: float) -> None:
    """模拟一次 agent 运行：工具步骤推进 + 正文流式输出。

    Args:
        card: 卡片客户端。
        draft: 卡片草稿。
        step_delay: 每个工具步骤的模拟耗时（秒）。
    """
    for call_id, name in _DEMO_STEPS:
        draft.set_tool(call_id, name, "running")
        await card.push(draft)
        await asyncio.sleep(step_delay)
        draft.set_tool(call_id, name, "completed")
        await card.push(draft)

    for chunk in _DEMO_ANSWER_CHUNKS:
        draft.append_answer(chunk)
        await card.push(draft)
        await asyncio.sleep(step_delay / 2)

    await card.push(draft, finished=True)


def _build_target(args: argparse.Namespace) -> dict[str, str]:
    """按参数构造投放目标。

    Args:
        args: 命令行参数。

    Returns:
        含 ``openConversationId`` 或 ``receiverUserId`` 的目标字典。
    """
    if args.user_id:
        return {"receiverUserId": args.user_id}
    return {"openConversationId": args.open_conversation_id}


def load_config_credentials(config_path: str) -> tuple[str, str]:
    """从实例配置文件读取钉钉凭证。

    只读取 ``channels.channel_dingtalk`` 下的 ``client_id`` / ``client_secret``，
    避免凭证出现在命令行历史与进程列表里。

    Args:
        config_path: nanobee 实例配置文件路径（如 ``/nanobee-data/<实例>/config.yaml``）。

    Returns:
        ``(client_id, client_secret)``；读取失败时返回空串元组。
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        logger.exception("读取配置文件失败: {}", config_path)
        return "", ""
    section = (data.get("channels") or {}).get("channel_dingtalk") or {}
    return str(section.get("client_id", "")), str(section.get("client_secret", ""))


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="钉钉 v2 结构化 AI 卡片 demo")
    parser.add_argument("--config", default="", help="nanobee 实例 config.yaml 路径")
    parser.add_argument("--client-id", default=os.environ.get("DINGTALK_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("DINGTALK_CLIENT_SECRET", ""))
    parser.add_argument("--robot-code", default=os.environ.get("DINGTALK_ROBOT_CODE", ""))
    parser.add_argument("--open-conversation-id", default=os.environ.get("DINGTALK_OPEN_CONVERSATION_ID", ""))
    parser.add_argument("--user-id", default=os.environ.get("DINGTALK_USER_ID", ""))
    parser.add_argument("--template-id", default=V2_TEMPLATE_ID)
    parser.add_argument("--push-interval", type=float, default=DEFAULT_PUSH_INTERVAL)
    parser.add_argument("--step-delay", type=float, default=None, help="每个步骤的模拟耗时（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求体，不联网")
    return parser.parse_args()


async def _run_demo(args: argparse.Namespace) -> int:
    """执行一次 demo。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码。
    """
    step_delay = args.step_delay if args.step_delay is not None else (0.05 if args.dry_run else 1.0)
    target = _build_target(args)

    client_id, client_secret = args.client_id, args.client_secret
    if args.config and not (client_id and client_secret):
        loaded_id, loaded_secret = load_config_credentials(args.config)
        client_id = client_id or loaded_id
        client_secret = client_secret or loaded_secret
        logger.info("已从配置载入凭证: {}", args.config)
    # 与生产一致：robotCode 缺省取 client_id（见 sender.py 的卡片投放调用）
    robot_code = args.robot_code or client_id

    if not args.dry_run:
        if not (client_id and client_secret):
            logger.error("缺少凭证：请设置 DINGTALK_CLIENT_ID / CLIENT_SECRET / ROBOT_CODE，或使用 --dry-run")
            return 2
        if not target:
            logger.error("缺少投放目标：请设置 DINGTALK_OPEN_CONVERSATION_ID 或 DINGTALK_USER_ID")
            return 2

    draft = CardV2Draft()
    async with httpx.AsyncClient(timeout=30) as http:
        if args.dry_run:

            async def token_fn() -> str:
                return "DRY_RUN_TOKEN"

        else:
            manager = TokenManager(client_id, client_secret, http=http)

            async def token_fn() -> str:
                token = await manager.get_access_token()
                if not token:
                    raise RuntimeError("获取钉钉 access token 失败")
                return token

        card = DingTalkCardV2(
            http,
            token_fn,
            robot_code,
            target,
            template_id=args.template_id,
            dry_run=args.dry_run,
            push_interval=0.0 if args.dry_run else args.push_interval,
        )
        await card.create_and_deliver()
        await simulate_agent_run(card, draft, step_delay)

    logger.info("Demo 完成：card={}", card.out_track_id)
    return 0


def main() -> int:
    """入口。"""
    return asyncio.run(_run_demo(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
