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
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nanobee.kernel.context_sandbox_var import current_request_context
from nanobee.plugins import ToolPlugin
from nanobee.security.workspace_policy import require_path_within
from nanobee.utils.logger import logger

# 状态机合法状态
VALID_STATUSES = {"pending", "in_progress", "completed", "deleted"}


class TaskStoreError(Exception):
    """任务存储不可用（文件损坏/结构非法/读取失败）。

    作为工具可预期错误上抛给 execute_tool 统一转为面向 LLM 的
    错误结果，避免静默返回空数据导致后续覆盖丢数据。
    """


# 允许推进的方向（简化版状态机，符合任务跟踪语义）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "completed", "deleted"},
    "in_progress": {"completed", "pending", "deleted"},
    "completed": {"in_progress", "pending", "deleted"},
    "deleted": set(),
}

_NS_RE = re.compile(r"[^0-9A-Za-z_.\-@]")


def _sanitize_ns(namespace: Any) -> str:
    """namespace 会拼进文件名，必须净化防路径穿越。

    LLM 可能传非字符串（如 namespace=123），统一 str() 兜底后再净化，
    与 subject/description 的处理保持一致，避免 TypeError 直接抛给框架。
    """
    # 正则已将 / 及其它路径字符统一替换为 _，净化结果不可能含 / 或 ..，
    # 天然阻断绝对路径与跳出数据目录
    return _NS_RE.sub("_", str(namespace)).strip("._") or "default"


class ToolTaskConfig(BaseModel):
    """tool_task 插件声明式配置。

    Attributes:
        data_dir: 任务数据目录。仅用于显式覆盖存储位置；
            空字符串（默认）表示存储在用户上下文内：
            <context_root>/task/<namespace>.json，
            与沙箱边界（context_root）保持一致。
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
        """初始化插件：解析配置的数据目录（可选覆盖）。

        默认不在此处定死存储根——运行时优先使用 per-request 的
        context_root（沙箱边界内），见 _resolve_base_dir()。
        仅当用户显式配置 data_dir 时，才在启动期解析该覆盖值。
        """
        super().initialize(kernel)
        cfg = self.config.data_dir if self.config else ""
        if cfg:
            base = Path(cfg).expanduser()
            if not base.is_absolute() and self.kernel is not None:
                base = Path(self.kernel.data_dir).expanduser() / base
            self._data_dir = base
            logger.info("Task 插件初始化完成，数据目录(配置覆盖): {}", base)
        else:
            self._data_dir = None
            logger.info("Task 插件初始化完成，数据目录: <context_root>/task/")

    # ------------------------------------------------------------------
    # 存储层
    # ------------------------------------------------------------------

    def _resolve_base_dir(self, context_id: str) -> Path:
        """解析存储根目录，并强制落在沙箱边界内。

        优先级：
        1. 配置的 data_dir（管理员显式覆盖，视为可信）；
        2. per-request 的 context_root（沙箱可写边界），
           即 <context_root>/task/，与 tool_cron / audit_logger 的
           存储约定一致，任务数据天然受沙箱管控且随用户目录清理。

        注意：不能默认写 kernel.data_dir —— 那在沙箱边界之外，
        构成沙箱越界写入。仅当 context_root 未注入（boot/测试）
        时才回退 kernel.data_dir，并保留边界校验。

        Args:
            context_id: 用户上下文 ID（回退场景下用于目录隔离）。

        Returns:
            绝对路径。

        Raises:
            SandboxViolationError: context_root 分支解析结果落在边界之外。
                注意：显式配置 data_dir 的分支视为管理员可信覆盖，
                仅做 resolve 归一（防 .. / symlink 逃逸出该目录本身），
                不做沙箱边界校验。
        """
        if self._data_dir is not None:
            # 配置覆盖：管理员显式指定，视为可信；仅 resolve 归一
            # （防 .. / symlink 逃逸出该目录本身），不做沙箱边界校验
            return Path(self._data_dir).expanduser().resolve(strict=False)
        context_root = self.context_root
        if context_root is not None:
            base = Path(context_root).expanduser().resolve(strict=False) / "task"
            # 硬边界校验：resolve 后必须仍落在 context_root 内
            return require_path_within(base, context_root, message="task 存储目录越界拦截")
        # 回退：无 per-request 上下文（测试/boot）。
        # 注意 tool_cron 无此回退（它要求 context_root 或显式 <data_dir>/cron/）
        data_dir = (
            Path(self.kernel.data_dir).expanduser()
            if self.kernel and hasattr(self.kernel, "data_dir")
            else Path.cwd()
        )
        return data_dir.resolve(strict=False) / "task" / _sanitize_ns(context_id)

    def _store_path(self, context_id: str, namespace: str) -> Path:
        """解析任务存储路径。

        - 默认（无配置覆盖）：<context_root>/task/<ns>.json，
          context_root 本身即 per-user 隔离单元，无需再拼 context_id；
        - 配置覆盖 data_dir 时：<data_dir>/<context_id>/<ns>.json；
        - 无 per-request 上下文回退时：<data_dir>/task/<context_id>/<ns>.json
          （<ctx>/ 由 _resolve_base_dir 拼接，此处不可重复拼接）。

        namespace 由 LLM 传入，净化后拼进文件名防路径穿越。
        """
        ns = _sanitize_ns(namespace)
        base = self._resolve_base_dir(context_id)
        if self._data_dir is not None:
            # 配置覆盖目录本身不做用户隔离，在此显式拼 <ctx>/ 保持 per-user 独立
            return base / _sanitize_ns(context_id) / f"{ns}.json"
        # context_root 场景：base 即 <context_root>/task/，本身按用户隔离；
        # 回退场景（kernel.data_dir）由 _resolve_base_dir 已拼 <ctx>/，
        # 两种情况均无需再拼 context_id
        return base / f"{ns}.json"

    @staticmethod
    def _load(path: Path) -> dict:
        """读取任务存储。

        文件不存在时返回空字典（正常首次使用）。

        Raises:
            TaskStoreError: 文件存在但无法解析（损坏/非法结构）。
                此类错误必须上抛而非静默返回空字典 —— 否则下一次
                _save 会用空数据整体覆盖，已存任务永久丢失且 LLM
                只看到 count: 0。
        """
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("task store {} 解析失败: {}", path, e)
            raise TaskStoreError(f"任务存储文件损坏，无法解析: {path.name}（{e}）") from e
        except OSError as e:
            logger.warning("task store {} 读取失败: {}", path, e)
            raise TaskStoreError(f"任务存储文件读取失败: {path.name}（{e}）") from e
        if not isinstance(data, dict):
            logger.warning("task store {} 结构非法: {}", path, type(data).__name__)
            raise TaskStoreError(f"任务存储文件结构非法（期望 JSON 对象）: {path.name}")
        return data

    @staticmethod
    def _save(path: Path, tasks: dict) -> None:
        """原子写：先写临时文件再 os.replace，避免并发损坏。

        mkstemp 默认 0600；若目标文件已存在，沿用其原权限位，
        避免每次写入都把管理员调整过的权限重置回 0600。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # 记录既有权限位（不存在则用 mkstemp 默认 0600）
        prev_mode: int | None = None
        try:
            prev_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            prev_mode = None

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
            if prev_mode is not None:
                os.chmod(tmp, prev_mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # _locks 上界：key 为"用户 × namespace"，只增不减会在长跑 gateway 中缓慢泄漏。
    # 超限时回收未持有的锁（锁仅在单次工具调用期间持有，回收不影响正确性）。
    _MAX_LOCKS = 1024

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is not None:
            return lock
        if len(self._locks) >= self._MAX_LOCKS:
            # 只回收未被持有的锁，避免打断正在进行的写入
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        lock = asyncio.Lock()
        self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------
    # 工具定义
    # ------------------------------------------------------------------

    def get_tools(self) -> list[dict[str, Any]]:
        """返回 task 工具的 OpenAI function schema 列表。

        description 既写"是什么"，也写"何时调用/何时不要调用"——
        触发语义必须贴在 LLM 的工具调用决策点旁（本方法每轮 LLM 调用都会
        被现取现发），而不是写进 core.md 这类长 system prompt 或用户管控文件。
        阈值（几个步骤算复杂）刻意不做硬编码，交由 LLM 结合情境自主判断。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "task_create",
                    "description": (
                        "创建一条新任务（状态 pending），把请求拆成可跟踪的步骤清单。"
                        "应在以下情况调用：① 请求包含多个相互独立的步骤，需要分几轮才做完；"
                        "② 需要改动多个文件或跨多处配置；"
                        "③ 用户一次提出多项要求；"
                        "④ 多轮对话中需要跨轮记住还剩什么没做。"
                        "步骤数由你自主判断，不必凑数。"
                        "以下情况不要调用：单步操作、一次工具调用即可完成；"
                        "纯问答、闲聊、解释说明；只是复述或总结已有信息。"
                        "注意：若要记住的是事实或结论而非待办步骤，"
                        "应写入 memory/，不要建任务。"
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
                                "description": (
                                    "任务清单命名空间，用于隔离不同主题的清单；"
                                    '默认 "default"（同一用户跨会话共享）。'
                                    "如需会话级隔离，请显式传会话 ID 作为 namespace"
                                ),
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
                        "应在以下情况调用：① 开始动手做某项任务前，先置为 in_progress；"
                        "② 该项任务做完后，置为 completed，不要攒着批量改；"
                        "③ 任务被放弃或需求变更时，置为 deleted，或改标题/描述。"
                        "同一任务仅在其状态真正变化时更新，避免无意义的重复调用。"
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
                    "description": (
                        "列出任务清单，可按状态过滤。"
                        "应在以下情况调用：① 汇报进度、总结产出前，先取实时状态，不要凭记忆口述；"
                        "② 准备创建新任务前，先看是否已有同类任务，避免重复建单；"
                        "③ 需要确认还剩哪些未完成时。"
                    ),
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
                                "description": '任务清单命名空间，需与创建时一致，默认 "default"',
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
                    "description": (
                        "查看单条任务详情。"
                        "应在以下情况调用：需要某项任务的完整描述，或确认其当前状态时。"
                        "只想知道有哪些任务请用 task_list，不必逐条 task_get。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "任务 ID",
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
            try:
                if tool_name == "task_create":
                    return self._task_create(path, **kwargs)
                if tool_name == "task_update":
                    return self._task_update(path, **kwargs)
                if tool_name == "task_list":
                    return self._task_list(path, **kwargs)
                return self._task_get(path, **kwargs)
            except TaskStoreError as e:
                # 存储不可用时把错误透传给 LLM，而不是让它看到空清单
                return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

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

        # task 即 tasks[task_id] 的同一引用，就地修改后无需回写
        self._save(path, tasks)
        return json.dumps({"ok": True, "task": self._public(task)}, ensure_ascii=False)

    def _task_list(self, path: Path, **kwargs: Any) -> str:
        status = str(kwargs.get("status", "") or "")
        if status and status not in VALID_STATUSES:
            # 与 task_update 保持一致：非法状态显式报错。
            # 静默返回空清单会让 LLM 误判"没有任务"从而重复创建。
            return json.dumps(
                {"ok": False, "error": f"非法状态 {status!r}，合法集合: {sorted(VALID_STATUSES)}"},
                ensure_ascii=False,
            )
        tasks = self._load(path)
        items = [self._public(t) for t in tasks.values()]
        if status:
            items = [t for t in items if t["status"] == status]
        items.sort(key=lambda t: t["id"])
        result: dict[str, Any] = {"ok": True, "count": len(items), "tasks": items}
        if not items:
            # 空清单时附带可用 namespace，帮助 LLM 区分"真没任务"与"namespace 传错"
            hint = self._namespace_hint(path)
            if hint:
                result["available_namespaces"] = hint
        return json.dumps(result, ensure_ascii=False)

    def _task_get(self, path: Path, **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id", ""))
        task = self._load(path).get(task_id)
        if task is None:
            # 未命中常见于 namespace 传错（默认 default 是同用户跨会话共享）。
            # 提示实际可用的 namespace，避免 LLM 误判为任务不存在。
            hint = self._namespace_hint(path)
            error = f"task {task_id} 不存在"
            if hint:
                error += f"；当前 namespace 下无此任务，可用 namespace: {hint}"
            return json.dumps({"ok": False, "error": error}, ensure_ascii=False)
        return json.dumps({"ok": True, "task": self._public(task)}, ensure_ascii=False)

    @staticmethod
    def _namespace_hint(path: Path) -> str:
        """列出同目录下已存在的 namespace 文件名（不含 .json）。"""
        try:
            names = sorted(p.stem for p in path.parent.glob("*.json"))
        except OSError:
            return ""
        return ", ".join(names)
