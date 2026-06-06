"""Heartbeat 服务 - 后台定时唤醒 Agent 检查待处理任务。

借鉴 nanobot 的 Phase 1/Phase 2 两阶段设计:
- Phase 1 (决策): 读取 WORKFLOW.md,通过 LLM 虚拟工具调用判断是否有任务
- Phase 2 (执行): 仅在 Phase 1 返回 run 时才执行任务

核心优势:
- 不依赖自由文本解析,用结构化决策避免不可靠的 HEARTBEAT_OK token
- 可插拔回调,与 nanobee 的 Plugin Hook 机制天然契合
- 结果过滤,过滤掉内部推理过程,只投递有效结果给用户
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

import logging

logger = logging.getLogger(__name__)


# 虚拟工具定义:LLLM 通过此工具返回结构化决策
_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "汇报心跳决策,在审查任务后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = 无任务,run = 有活跃任务",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "活跃任务的自然语言摘要(run 时必填)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    后台定时唤醒服务,定期让 Agent 检查待处理任务。

    使用示例:
        service = HeartbeatService(
            workspace=Path("~/.nanobee").expanduser(),
            provider=provider,
            model="claude-sonnet-4-20250514",
            on_execute=execute_task,
            on_notify=notify_user,
            interval_s=300,  # 5 分钟
        )
        await service.start()
        # ... 后台运行 ...
        await service.stop()
    """

    def __init__(
        self,
        workspace: Path,
        provider: Any | None = None,
        model: str | None = None,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
    ):
        """初始化 HeartbeatService

        Args:
            workspace: 工作目录路径
            provider: LLM Provider 实例(与 model 二选一,或提供 llm_provider)
            model: 模型名称
            on_execute: 执行任务的回调,接收任务描述,返回执行结果
            on_notify: 通知用户的回调,接收通知内容
            interval_s: 唤醒间隔(秒),默认 30 分钟
            enabled: 是否启用心跳服务
            timezone: 时区,默认使用系统时区
        """
        self.workspace = workspace
        self._provider = provider
        self._model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None
        self._llm_provider: Any | None = None

    @property
    def workflow_file(self) -> Path:
        """工作流文件路径"""
        return self.workspace / "WORKFLOW.md"

    def _read_workflow_file(self) -> str | None:
        """读取 WORKFLOW.md 文件内容

        Returns:
            文件内容,如果文件不存在或读取失败则返回 None
        """
        if self.workflow_file.exists():
            try:
                return self.workflow_file.read_text(encoding="utf-8")
            except Exception:
                logger.exception("读取 WORKFLOW.md 失败")
                return None
        return None

    async def _get_llm(self) -> Any:
        """获取 LLM 实例(懒初始化)

        Returns:
            LLM Provider 实例
        """
        if self._llm_provider is not None:
            return self._llm_provider

        if self._provider is None:
            raise ValueError("HeartbeatService 需要 provider 或 llm_provider")

        # 延迟导入,避免循环依赖
        from nanobot.providers.base import LLMProvider

        if not isinstance(self._provider, LLMProvider):
            # 尝试从 provider 获取 chat 方法
            if not hasattr(self._provider, "chat_with_retry"):
                raise ValueError("Provider 必须有 chat_with_retry 方法")

        self._llm_provider = self._provider
        return self._llm_provider

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: 通过 LLM 虚拟工具调用决策是否执行任务

        Args:
            content: WORKFLOW.md 的内容

        Returns:
            (action, tasks) 元组,action 为 'skip' 或 'run',tasks 为任务摘要
        """
        # 延迟导入
        from nanobee.utils.helpers import current_time_str

        llm = await self._get_llm()
        model = self._model or getattr(llm, "model", None) or "claude-sonnet-4-20250514"

        response = await llm.chat_with_retry(
            messages=[
                {"role": "system", "content": "你是心跳代理,请调用 heartbeat 工具汇报决策。"},
                {"role": "user", "content": (
                    f"当前时间: {current_time_str(self.timezone)}\n\n"
                    "请审查以下 WORKFLOW.md,判断是否有活跃任务。\n\n"
                    f"{content}"
                )},
            ],
            tools=_HEARTBEAT_TOOL,
            model=model,
        )

        # 如果没有工具调用,跳过
        if not response.should_execute_tools:
            if response.has_tool_calls:
                logger.warning(
                    "忽略心跳工具调用,finish_reason='{}'",
                    response.finish_reason,
                )
            return "skip", ""

        # 解析工具调用参数
        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """启动心跳服务"""
        if not self.enabled:
            logger.info("心跳服务已禁用")
            return
        if self._running:
            logger.warning("心跳服务已在运行")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("心跳服务已启动(间隔 {}s)", self.interval_s)

    def stop(self) -> None:
        """停止心跳服务"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("心跳服务已停止")

    async def _run_loop(self) -> None:
        """心跳主循环"""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("心跳服务异常")

    @staticmethod
    def _is_deliverable(response: str) -> bool:
        """检查心跳响应是否适合投递给用户

        过滤两类不良输出:
        1. 最终化回退: Runner 命中空响应重试,产生兜底错误消息
        2. 泄露的内部推理: 模型反射内部文件名、决策逻辑或元评论

        Args:
            response: Agent 执行结果

        Returns:
            是否适合投递
        """
        text = response.lower()

        # Runner 最终化回退
        if "couldn't produce a final answer" in text:
            return False

        # 泄露的内部推理模式
        leaked_patterns = [
            "workflow.md",
            "awareness.md",
            "judgment call:",
            "decision logic",
            "valid options are",
            "my instructions",
            "i am supposed to",
            "strict heartbeat interpretation",
        ]
        if any(pattern in text for pattern in leaked_patterns):
            return False

        return True

    async def _tick(self) -> None:
        """执行单次心跳检查"""
        content = self._read_workflow_file()
        if not content:
            logger.debug("WORKFLOW.md 不存在或为空")
            return

        logger.info("心跳: 检查任务...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("心跳: 无需处理")
                return

            logger.info("心跳: 发现任务,执行中...")
            if self.on_execute:
                response = await self.on_execute(tasks)

                if not response:
                    logger.info("心跳: 执行无响应")
                    return

                if not self._is_deliverable(response):
                    logger.info(
                        "心跳: 抑制不可投递响应 (%s)",
                        response[:80],
                    )
                    return

                # 评估是否通知用户
                should_notify = await self._evaluate_notification(response, tasks)
                if should_notify and self.on_notify:
                    logger.info("心跳: 完成,投递响应")
                    await self.on_notify(response)
                else:
                    logger.info("心跳: 由后置评估静默")
        except Exception:
            logger.exception("心跳执行失败")

    async def _evaluate_notification(self, response: str, tasks: str) -> bool:
        """评估是否应该将结果通知给用户

        Args:
            response: Agent 执行结果
            tasks: 任务描述

        Returns:
            是否应该通知
        """
        # 简化版:默认通知(可后续扩展为 LLM 评估)
        return True

    async def trigger_now(self) -> str | None:
        """手动触发心跳检查

        Returns:
            执行结果,如果没有任务或无法执行则返回 None
        """
        content = self._read_workflow_file()
        if not content:
            return None

        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            return None

        return await self.on_execute(tasks)
