"""Tool Task 插件 — 任务分解与状态跟踪。

语义对齐 CodeBuddy/Claude Code 的 Task 系统（Todo V2）：
- task_create(subject, description, active_form)  创建 pending 任务
- task_update(task_id, ...)                       推进状态机 / 修改字段
- task_list(status)                               列出任务（可按状态过滤）
- task_get(task_id)                               查看单条详情

数据按 context_id（用户）隔离，JSON 文件原子写持久化（tmp + os.replace），
per-context asyncio.Lock 保证并发安全。状态机：
    pending -> in_progress -> completed（deleted 软删除）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nanobee.kernel.context_sandbox_var import current_request_context
from nanobee.plugins import ToolPlugin
from nanobee.utils.logger import logger

# 状态机合法状态
VALID_STATUSES = {"pending", "in_progress", "completed", "deleted"}
# 允许推进的方向（简化版状态机，符合任务跟踪语义）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "completed", "deleted"},
    "in_progress": {"completed", "pending", "deleted"},
    "completed": {"in_progress", "pending", "deleted"},
    "deleted": set(),
}

_NS_RE = re.compile(r"[^0-9A-Za-z_.\-@]")


def _sanitize_ns(namespace: str) -> str:
    """namespace 会拼进文件名，必须净化防路径穿越。"""
    ns = _NS_RE.sub("_", namespace)
    ns = ns.strip("._") or "default"
    # 阻断绝对路径 / 跳出数据目录
    if ns.startswith("/") or ".." in ns.split("/"):
        ns = "default"
    return ns


class ToolTaskConfig(BaseModel):
    """tool_task 插件声明式配置。

    Attributes:
        data_dir: 任务数据目录。相对路径基于 kernel.data_dir 解析；
            空字符串表示使用 <kernel.data_dir>/task/。
    """

    data_dir: str = ""


class ToolTaskPlugin(ToolPlugin):
    """任务分解与状态跟踪工具插件。

    提供 task_create / task_update / task_list / task_get 四个工具。
    通过 CURRENT_REQUEST_CONTEXT ContextVar 按 turn 获取会话信息，
    按 context_id 隔离存储目录，多用户并发互不干扰。
    """

    config_cls = ToolTaskConfig

    def __init__(self, metadata: Any = None):
        super().__init__(metadata)
        self._locks: dict[str, asyncio.Lock] = {}
        self._data_dir: Path | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def initialize(self, kernel: Any) -> None:
        """初始化插件：解析数据目录。"""
        super().initialize(kernel)
        cfg = self.config.data_dir if self.config else ""
        if cfg:
            base = Path(cfg).expanduser()
            if not base.is_absolute() and self.kernel is not None:
                base = Path(self.kernel.data_dir).expanduser() / base
        else:
            base = Path(self.kernel.data_dir).expanduser() / "task" if self.kernel else Path.cwd() / "task"
        self._data_dir = base
        logger.info("Task 插件初始化完成，数据目录: {}", base)

    # ------------------------------------------------------------------
    # 存储层
    # ------------------------------------------------------------------

    def _store_path(self, context_id: str, namespace: str) -> Path:
        """解析任务存储路径：<data_dir>/<context_id>/<ns>.json。

        context_id 由框架注入（用户上下文隔离），namespace 由 LLM 传入
        （用户/会话/聊天级隔离），两级净化后拼进文件名。
        """
        ns = _sanitize_ns(namespace)
        ctx = _sanitize_ns(context_id)
        return (self._data_dir or Path.cwd() / "task") / ctx / f"{ns}.json"

    @staticmethod
    def _load(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            logger.warning("task store {} unreadable, starting empty", path)
            return {}

    @staticmethod
    def _save(path: Path, tasks: dict) -> None:
        """原子写：先写临时文件再 os.replace，避免并发损坏。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ------------------------------------------------------------------
    # 工具定义
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """返回 task 工具的 OpenAI function schema 列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "task_create",
                    "description": (
                        "创建一条新任务（状态 pending）。"
                        "用于将复杂请求拆解为可跟踪的步骤清单。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subject": {
                                "type": "string",
                                "description": '简洁可执行的动宾标题（如 "修复登录流程中的认证 bug"）',
                            },
                            "description": {
                                "type": "string",
                                "description": "详细说明要做什么",
                            },
                            "activeForm": {
                                "type": "string",
                                "description": '（可选）任务进行时展示的"进行中"措辞',
                            },
                            "namespace": {
                                "type": "string",
                                "description": '任务清单命名空间（用户/会话/聊天级隔离），默认 "default"',
                            },
                        },
                        "required": ["subject"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task_update",
                    "description": (
                        "更新任务字段和/或状态。"
                        "常规流转：pending -> in_progress -> completed。"
                        "状态只允许合法迁移；空字段表示不改动。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "任务 ID（由 task_create 返回）",
                            },
                            "status": {
                                "type": "string",
                                "enum": sorted(VALID_STATUSES),
                                "description": "目标状态，空表示不改动",
                            },
                            "subject": {
                                "type": "string",
                                "description": "新的标题，空表示不改动",
                            },
                            "description": {
                                "type": "string",
                                "description": "新的描述，空表示不改动",
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "新的进行时措辞，空表示不改动",
                            },
                            "namespace": {
                                "type": "string",
                                "description": '任务清单命名空间，需与创建时一致，默认 "default"',
                            },
                        },
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task_list",
                    "description": "列出任务清单，可按状态过滤。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": sorted(VALID_STATUSES),
                                "description": '按状态过滤（pending/in_progress/completed/deleted），空表示全部',
                            },
                            "namespace": {
                                "type": "string",
                                "description": '任务清单命名空间，默认 "default"',
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task_get",
                    "description": "查看单条任务详情。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "任务 ID",
                            },
                            "namespace": {
                                "type": "string",
                                "description": '任务清单命名空间，默认 "default"',
                            },
                        },
                        "required": ["task_id"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """执行 task 工具。

        Args:
            tool_name: 工具名称（task_create / task_update / task_list / task_get）
            **kwargs: 工具参数

        Returns:
            执行结果 JSON 字符串

        Raises:
            ValueError: 工具不存在
        """
        if tool_name not in ("task_create", "task_update", "task_list", "task_get"):
            raise ValueError(f"未知工具: {tool_name}")

        # 从 per-turn ContextVar 获取路由上下文（线程安全）
        rctx = current_request_context()
        if rctx is None:
            return f"错误：无法获取当前会话上下文，{tool_name} 未能执行"

        namespace = kwargs.get("namespace") or "default"
        path = self._store_path(rctx.context_id, namespace)
        lock = self._lock(str(path))

        async with lock:
            if tool_name == "task_create":
                return self._task_create(path, **kwargs)
            if tool_name == "task_update":
                return self._task_update(path, **kwargs)
            if tool_name == "task_list":
                return self._task_list(path, **kwargs)
            return self._task_get(path, **kwargs)

    # ------------------------------------------------------------------
    # 工具实现（调用方已持锁）
    # ------------------------------------------------------------------

    @staticmethod
    def _public(task: dict) -> dict:
        return {k: task.get(k, "") for k in ("id", "subject", "description", "activeForm", "status")}

    def _task_create(self, path: Path, **kwargs: Any) -> str:
        subject = str(kwargs.get("subject", "")).strip()
        if not subject:
            return json.dumps({"ok": False, "error": "subject 不能为空"}, ensure_ascii=False)
        tasks = self._load(path)
        tid = "T" + uuid.uuid4().hex[:8]
        tasks[tid] = {
            "id": tid,
            "subject": subject,
            "description": str(kwargs.get("description", "")),
            "activeForm": str(kwargs.get("activeForm", "")),
            "status": "pending",
        }
        self._save(path, tasks)
        return json.dumps({"ok": True, "task": self._public(tasks[tid])}, ensure_ascii=False)

    def _task_update(self, path: Path, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id", ""))
        tasks = self._load(path)
        task = tasks.get(task_id)
        if task is None:
            return json.dumps({"ok": False, "error": f"task {task_id} 不存在"}, ensure_ascii=False)

        status = str(kwargs.get("status", "") or "")
        if status:
            if status not in VALID_STATUSES:
                return json.dumps(
                    {"ok": False, "error": f"非法状态 {status!r}，合法集合: {sorted(VALID_STATUSES)}"},
                    ensure_ascii=False,
                )
            cur = task["status"]
            if status != cur and status not in ALLOWED_TRANSITIONS.get(cur, set()):
                return json.dumps(
                    {"ok": False, "error": f"非法状态迁移 {cur} -> {status}"},
                    ensure_ascii=False,
                )
            task["status"] = status

        subject = str(kwargs.get("subject", "") or "")
        if subject.strip():
            task["subject"] = subject.strip()
        active_form = str(kwargs.get("activeForm", "") or "")
        if active_form:
            task["activeForm"] = active_form
        description = str(kwargs.get("description", "") or "")
        if description:
            task["description"] = description

        tasks[task_id] = task
        self._save(path, tasks)
        return json.dumps({"ok": True, "task": self._public(task)}, ensure_ascii=False)

    def _task_list(self, path: Path, **kwargs: Any) -> str:
        status = str(kwargs.get("status", "") or "")
        tasks = self._load(path)
        items = [self._public(t) for t in tasks.values()]
        if status:
            items = [t for t in items if t["status"] == status]
        items.sort(key=lambda t: t["id"])
        return json.dumps({"ok": True, "count": len(items), "tasks": items}, ensure_ascii=False)

    def _task_get(self, path: Path, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id", ""))
        task = self._load(path).get(task_id)
        if task is None:
            return json.dumps({"ok": False, "error": f"task {task_id} 不存在"}, ensure_ascii=False)
        return json.dumps({"ok": True, "task": self._public(task)}, ensure_ascii=False)
