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
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nanobee.exceptions import SandboxViolationError
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

# 文件名安全字符：Unicode 字母数字（\w，含中文等）、下划线、点、连字符、@。
# 注意不要用 [^0-9A-Za-z_.\-@] 这类 ASCII 白名单 —— 那会把中文 namespace
# 静默改写成 default，任务被悄悄写进另一个清单且不报错（隐蔽数据错分）。
_NS_UNSAFE_RE = re.compile(r"[^\w.\-@]", re.UNICODE)

# 单段文件名上限（字节）与哈希后缀长度：超长 namespace 在 _truncate_filename
# 里按字节截断 + 内容哈希兜底。截断只能在这一处做统一处理——若提前在
# 净化阶段按固定字符数砍，超长输入的不同 namespace 会砍成同一个前缀，
# 最终落到同一个文件（长度有界 / 稳定 / 不碰撞，三者必须同时成立）。
_NAME_MAX_BYTES = 255
_HASH_SUFFIX_LEN = 9  # "-" + sha1 前 8 位


def _truncate_filename(name: str, suffix: str) -> str:
    """把文件名主体压到文件系统单段上限内，且保持稳定、不碰撞。

    超长 namespace（LLM 常把整段会话摘要当 namespace 传入）原样拼文件名会在
    _save 抛 OSError(ENAMETOOLONG) 穿透 execute_tool。截断后追加内容哈希后缀，
    保证 ① 长度有界；② 同一输入映射同一文件名；③ 不同输入不因截断而碰撞。

    Args:
        name: 文件名主体（已净化，可能含多字节字符）。
        suffix: 扩展名（含点，如 ".json"）。

    Returns:
        长度受限的文件名（含扩展名）。
    """
    budget = _NAME_MAX_BYTES - len(suffix.encode("utf-8"))
    encoded = name.encode("utf-8")
    if len(encoded) <= budget:
        return name + suffix
    digest = hashlib.sha1(encoded).hexdigest()[:_HASH_SUFFIX_LEN - 1]
    keep = budget - _HASH_SUFFIX_LEN
    # 按字节裁剪可能切碎多字节字符，errors="ignore" 丢弃残字节后回到合法字符边界
    truncated = encoded[:keep].decode("utf-8", errors="ignore")
    while len(truncated.encode("utf-8")) > keep:
        truncated = truncated[:-1]
    return f"{truncated}-{digest}{suffix}"


def _sanitize_ns(namespace: Any) -> str:
    """namespace 会拼进文件名，必须净化防路径穿越。

    - 非字符串（如 namespace=123）走 str() 兜底，不把 TypeError 抛给框架；
    - 路径分隔符等不安全字符统一替换为 _，首尾的 . / _ 一并裁掉，
      天然阻断绝对路径与 ".." 跳出数据目录；
    - Unicode 字母数字（含中文）保留原样：静默改写成 default 属于隐蔽数据错分；
    - 此处**不**做长度截断，长度统一交给 _truncate_filename 按字节处理。
    """
    cleaned = _NS_UNSAFE_RE.sub("_", str(namespace)).strip("._")
    return cleaned or "default"


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
        # 锁映射：key 是"事件循环 × 规范化存储路径"，value 是该 loop 内的互斥锁。
        #
        # 为什么按 loop 分桶：nanobee 每次 execute_tool 在各自的 asyncio.run()
        # 里执行（见 tests 的 _run_async、以及部分调用点），同一 key 会跨越不同
        # loop 复用。若共用一个 asyncio.Lock，则"第一个 loop 创建的锁"在后续
        # （可能并发的）loop 里会因 get_loop 不一致而抛 RuntimeError。
        # 分桶后：同 loop 内严格互斥；跨 loop 视为无并发（无共享事件循环即
        # 无法真正并发），符合原 per-context 锁的语义。
        #
        # 注意"回收空闲锁"的实现：不能用 lock.locked() 判定空闲后把整张映射
        # 重建，那会在"取锁→acquire 之间"的让出窗口里把在用的锁丢掉，导致
        # 同一 namespace 拿到两把不同的锁、互斥失效（评审 P0-1）。
        self._locks: dict[tuple[Any, str], asyncio.Lock] = {}
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
            root = Path(context_root).expanduser().resolve(strict=False)
            base = root / "task"
            # 先 resolve 再校验：task 若被替换成指向边界外的 symlink，这里即可拦截
            # （直接把未解析的 base 交给 require_path_within 会静默放过 symlink）
            verified = require_path_within(
                base.resolve(strict=False), root, message="task 存储目录越界拦截"
            )
            # 目录不存在时返回未解析路径：目录创建交给实际写入方，
            # 避免"只查询"也建目录，也避免此处的 resolve 在异常场景抛错
            return verified if verified.exists() else base
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

        namespace 由 LLM 传入，净化后拼进文件名防路径穿越；
        超长时由 _truncate_filename 截断加哈希，避免 OSError 穿透到框架层。
        """
        filename = _truncate_filename(_sanitize_ns(namespace), ".json")
        base = self._resolve_base_dir(context_id)
        if self._data_dir is not None:
            # 配置覆盖目录本身不做用户隔离，在此显式拼 <ctx>/ 保持 per-user 独立
            return base / _sanitize_ns(context_id) / filename
        # context_root 场景：base 即 <context_root>/task/，本身按用户隔离；
        # 回退场景（kernel.data_dir）由 _resolve_base_dir 已拼 <ctx>/，
        # 两种情况均无需再拼 context_id
        return base / filename

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

    # 锁映射上界：key 为"loop × 存储路径"，只增不减会在长跑 gateway 中缓慢泄漏。
    _MAX_LOCKS = 1024

    @staticmethod
    def _lock_key(path: Path) -> str:
        """用解析后的路径做 key：不同写法指向同一文件时共享同一把锁。"""
        try:
            return str(path.resolve(strict=False))
        except OSError:
            return str(path)

    def _lock(self, key: str) -> asyncio.Lock:
        """取（必要时建）当前事件循环内该存储路径的互斥锁。

        并发安全说明（评审 P0-1）：
        - 绝不回收"在用锁"。原实现按 ``lock.locked()`` 判空闲后把整张映射
          重建，而协程从取锁到 acquire 之间会让出控制权（execute_tool 里
          就有 ``async with lock``），该窗口内 locked() 同为 False，
          锁会被当作空闲丢掉 → 同一 namespace 拿到两把不同的锁、互斥失效。
          这里改为：调用方在持有锁期间绝不再调用 _lock，因此本函数看到的
          锁必然是空闲的，不存在"误删在用锁"的窗口。
        - 超限时按 FIFO 淘汰最旧的条目：dict 保持插入序，直接丢掉前面
          若干个即可。不用"挑空闲的丢"来实现——那需要判断锁是否在用，
          而 asyncio.Lock 没有公开的等待者查询接口（_waiters 是私有属性），
          判据一旦不完整就会连在用锁一起丢，正是原实现的坑。
        - 淘汰 N 个而不是清空：清空会把当前正在外部持有的锁也丢掉
          （如调用方刚取到锁、尚未进入 async with 的窗口），
          之后同一 key 会拿到新锁 → 互斥失效。
        - 建立新锁后重新取一次字典条目，保证返回的锁一定登记在表中，
          避免"返回的锁已被淘汰"的错配。
        """
        full_key = (id(asyncio.get_running_loop()), key)
        lock = self._locks.get(full_key)
        if lock is not None:
            return lock
        excess = len(self._locks) - self._MAX_LOCKS + 1
        if excess > 0:
            # 按插入序淘汰最旧的一批；被淘汰的锁即使仍被外部持有，
            # 也只是退化为"少一段互斥"，不会像清空那样把在用锁一起丢掉
            for stale in list(self._locks)[:excess]:
                del self._locks[stale]
        lock = asyncio.Lock()
        self._locks[full_key] = lock
        return self._locks[full_key]

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
        try:
            path = self._store_path(rctx.context_id, namespace)
        except SandboxViolationError as e:
            # 存储路径越界（如 task 目录被替换成指向边界外的 symlink）：
            # 与"存储不可用"同样收敛成 LLM 可读结果，不冒框架级异常
            logger.warning("task 存储路径越界: {}", e)
            return json.dumps(
                {"ok": False, "error": f"任务存储路径越界，已拒绝访问：{e}"},
                ensure_ascii=False,
            )
        lock = self._lock(self._lock_key(path))

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
            except OSError as e:
                # 磁盘/权限/文件名等 IO 异常同样收敛为工具结果，
                # 不让 OSError 穿透框架层（如超长文件名的 ENAMETOOLONG）
                logger.warning("task 存储 IO 失败 {}: {}", path, e)
                return json.dumps(
                    {"ok": False, "error": f"任务存储访问失败：{e}"}, ensure_ascii=False
                )

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
        # 空清单本身即正常结果（确实没有任务），不再挂 available_namespaces：
        # 该字段只在"疑似 namespace 传错"的未命中场景（task_get）才有信息量，
        # 挂在常规成功路径上只会给正常场景平白增加噪音。
        result: dict[str, Any] = {"ok": True, "count": len(items), "tasks": items}
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

    # hint 最多列出的 namespace 个数：错误串整体进 LLM 上下文，必须封顶
    _MAX_HINT_NAMESPACES = 5

    @classmethod
    def _namespace_hint(cls, path: Path) -> str:
        """列出同目录下可作为 namespace 的清单名。

        过滤与截断（评审 P1：hint 的语义与代价）：
        - 只认严格形如 ``<ns>.json`` 的普通文件：排除 ``x.json.json``、
          ``x.json.bak`` 这类 stem 混入，也不跟随 symlink；
        - 列表封顶 _MAX_HINT_NAMESPACES 个，超出时只报总数。
        """
        try:
            names = sorted(
                p.name[: -len(".json")]
                for p in path.parent.glob("*.json")
                if p.is_file() and not p.name.endswith(".json.json")
            )
        except OSError:
            return ""
        if not names:
            return ""
        shown = names[: cls._MAX_HINT_NAMESPACES]
        if len(names) > len(shown):
            shown.append(f"等 {len(names)} 个")
        return ", ".join(shown)
