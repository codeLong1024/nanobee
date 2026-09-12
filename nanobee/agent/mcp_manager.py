"""MCP 连接管理器 — 每个 server 一个 owner task 管理连接生命周期。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from nanobee.agent.tools.base import Tool
from nanobee.agent.tools.registry import ToolRegistry
from nanobee.utils.logger import logger

# —— MCP 生命周期超时常量（公开：跨模块预算推导与测试注入的单一来源） ——
# 这些是机制层兜底参数（防挂死），不是用户可见策略；机制自带上界是项目规约。

# 单个 server 关闭（拆解）的执行上限：owner task 内部对 ``stack.aclose()`` 的
# 硬上界（见 _do_close）。超时中断拆解的取舍：可能留下半途的活跃 scope 与未
# 回收的子进程（泄漏到进程退出），但换来 owner task 可退出、登记可被摘除——
# 若不上界，owner 会永久卡死在拆解里，该 server 直到进程重启都无法重建连接
# （连接泄漏 + 永久失联双输，后者更贵）。
PER_SERVER_OP_TIMEOUT_S = 5.0

# 单次「连接尝试」的上限（机制层兜底）。与上面的拆解超时分开：一次连接包含
# 子进程冷启动（npx 解析/拉包）与 initialize 握手，需要比关闭宽裕；同时明显
# 短于 MCP SDK 默认的 60s 单请求超时，避免一个卡死的 server 把整轮对话拖住。
CONNECT_ATTEMPT_TIMEOUT_S = 30.0

# 调用方等待预算相对「内部执行上界之和」的调度余量：外部等待必须 **大于**
# 内部上界（指令入队、task 切换都花时间），否则即使在正常路径也会假超时，
# 把可自愈的等待放大成用户可见失败。
OP_WAIT_SLACK_S = 2.0

# 重连**调用方**的等待上限 = 连接预算 + 拆解预算 + 余量（机制层兜底）。
#
# 重连的执行体是 _do_close() + _open()，即「先拆旧连接，再完整走一遍建连」；
# 机制上它同时受 CONNECT_ATTEMPT_TIMEOUT_S（连接）与 PER_SERVER_OP_TIMEOUT_S
# （拆解）约束，因此调用方的等待预算必须不小于二者之和。否则会出现
# 「机制允许它做 35s、调用方只等 5s」的自相矛盾：慢启动 server（npx 冷启动）
# 的重连几乎必然假超时，调用方拿到 None 后工具回报 could not refresh session，
# 尽管 owner 稍后其实重连成功了。
#
# 代价是刻意的：工具调用最坏会阻塞这么久。取舍为「宁可等，也不要假报失败」——
# 假失败会把一次本可自愈的重连暴露成用户可见的工具报错。
RECONNECT_WAIT_TIMEOUT_S = CONNECT_ATTEMPT_TIMEOUT_S + PER_SERVER_OP_TIMEOUT_S + OP_WAIT_SLACK_S

# close() 对单个 owner 的等待上限 = 拆解预算 + 余量：拆解本体在 owner 内部受
# PER_SERVER_OP_TIMEOUT_S 约束，外部只需覆盖「内部上界 + 调度余量」。
CLOSE_WAIT_TIMEOUT_S = PER_SERVER_OP_TIMEOUT_S + OP_WAIT_SLACK_S

# 连接失败后的重试冷却期（机制层兜底）。kernel 每条消息都会调用 connect()，
# 若失败后立即重试，挂死型故障（stdio 起不来）会让每条消息都重新付出一次完整
# 连接尝试（最长 CONNECT_ATTEMPT_TIMEOUT_S 的阻塞）。冷却只作用于「为已消失
# 的 owner 新建连接」，不影响在途 owner 的等待与复用。
RETRY_COOLDOWN_S = 30.0


@dataclass
class _Op:
    """投递给 owner task 的指令。

    Args:
        kind: 指令类型，当前为 ``close`` 或 ``reconnect``。
        future: 指令完成时由 owner task 置位，调用方据此等待结果。
        tool_name: ``reconnect`` 指令需要刷新的工具名。
        stale_tool: 发起重连的那个（可能已过期的）工具对象，用于识别
            「已有别的调用方完成了重连」从而避免重复拆建。
    """

    kind: str
    future: asyncio.Future[Any]
    tool_name: str | None = None
    stale_tool: Tool | None = None


class _ServerOwner:
    """单个 MCP server 的常驻 owner task。

    为什么必须一个 server 一个 task：

    1. anyio 的 ``CancelScope`` 归属「进入它的那个 task」——anyio 4.13 的
       ``_backends/_asyncio.py`` 在退出时硬校验宿主 task 与「当前 task 的最内层
       scope」。跨 task 退出直接抛
       ``RuntimeError: Attempted to exit cancel scope in a different task ...``。
       因此连接的建立（enter）与关闭（exit）必须由同一个 task 完成。
    2. 更进一步：同一 task 内并发持有的多个 scope 严格嵌套，退出必须逆序，
       且无法只退出中间某个。所以「一个 task 管多个 server」在关闭语义上不成立
       —— 这正是每个 server 独占一个 owner task 的原因。

    本类即那个 task：连接的建立、关闭、重连都在 ``_serve`` 内完成，调用方只投递
    指令并等待结果。
    """

    def __init__(
        self,
        name: str,
        cfg: Any,
        registry: ToolRegistry,
        *,
        default_cwd: str | None = None,
    ) -> None:
        """初始化 owner。

        Args:
            name: 服务器名（配置键）。
            cfg: 该服务器的配置（dict 或 MCPServerConfig）。
            registry: 工具注册表，连接成功后 MCP 能力注册到此。
            default_cwd: MCP stdio 进程的默认工作目录。
        """
        self.name = name
        self._cfg = cfg
        self._registry = registry
        self._default_cwd = default_cwd
        self._ops: asyncio.Queue[_Op] = asyncio.Queue()
        self._ready: asyncio.Future[bool] | None = None
        self._stack: AsyncExitStack | None = None
        self._connected = False
        self._closing = False
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        """该 server 当前是否处于已连接状态。"""
        return self._connected

    @property
    def alive(self) -> bool:
        """owner task 是否仍在运行（连接/重连/关闭都可能在进行中）。"""
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """确保本 server 处于已连接状态，返回当前连接状态。

        幂等：连接进行中时等待同一结果（不会出现「首轮缺失 MCP 工具」的竞态）；
        owner 已退出时重新拉起（首次连接、重连失败后的重试、close 之后的复用）。

        注意：不能用「首次连接时的 ready 结果」代表当前状态——重连失败后该 server
        已经断开，返回过期结果会让 connect() 假报成功并放弃重试，使其永久失联。

        Returns:
            当前是否已连接。连接失败不抛出，由调用方决定重试策略。
        """
        if self._task is None or self._task.done():
            ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._ready = ready
            # 新连接会话从零开始：上一世的关闭请求不适用于本次。不复位的话，
            # 复活的 owner 会被「上一世的 close」永久豁免，二次 close 在守卫处
            # 直接返回，连接泄漏到进程退出。
            self._closing = False
            self._task = asyncio.create_task(
                self._serve(), name=f"nanobee-mcp-{self.name}",
            )
            # shield：某个调用方被取消不得连带取消共享的连接结果——否则 ready
            # 变成 cancelled 后，后续并发 connect() 会静默拿到过期的 False。
            return await asyncio.shield(ready)
        pending = self._ready
        if pending is not None and not pending.done():
            return await asyncio.shield(pending)  # 首次连接仍在进行中：等同一结果
        return self._connected

    async def request_close(self) -> None:
        """请求关闭；**首个**调用者返回时 owner task 已退出（连接确定拆完）。

        已请求过关闭或已退出的 owner 直接返回：并发的后续调用者不重复等待同
        一个挂死的拆解（登记是否摘除由 close() 以 ``not owner.alive`` 判定）。
        """
        task = self._task
        if self._closing or task is None or task.done():
            return
        self._closing = True
        await self._request("close")
        # 等 owner task 真正退出：只有它退出，连接才确定拆完，close() 才能安全摘除登记
        await task

    async def request_reconnect(
        self, tool_name: str, stale_tool: Tool | None = None,
    ) -> Tool | None:
        """请求在 owner task 内重连，返回刷新后的工具（失败返回 None）。

        带超时兜底：重连是在工具调用的 except 分支里 await 的，外层没有其它超时
        保护（``call_fn`` 的 wait_for 只护住第一次调用），stdio 启动卡死会一路
        挂死整个 turn。超时后指令仍由 owner 继续处理，只是调用方不再等待。

        预算用 ``RECONNECT_WAIT_TIMEOUT_S`` 而非拆解预算：重连内部会完整重走
        一次建连，只给拆解量级的预算会让慢启动 server 的重连必然假超时。
        """
        try:
            return await asyncio.wait_for(
                self._request("reconnect", tool_name=tool_name, stale_tool=stale_tool),
                timeout=RECONNECT_WAIT_TIMEOUT_S,
            )
        except TimeoutError:
            logger.error(
                "MCP server '{name}' 重连超时（{timeout}s），本次调用放弃等待",
                name=self.name,
                timeout=RECONNECT_WAIT_TIMEOUT_S,
            )
            return None

    async def _request(
        self, kind: str, tool_name: str | None = None, stale_tool: Tool | None = None,
    ) -> Tool | None:
        """投递指令并等待 owner task 处理完成。"""
        task = self._task
        if task is None or task.done():
            return None
        future = asyncio.get_running_loop().create_future()
        self._ops.put_nowait(_Op(kind, future, tool_name, stale_tool))
        return await future

    async def _serve(self) -> None:
        """owner task 主体：在本 task 内完成连接的 enter / exit。"""
        try:
            stack = await self._open()
            if stack is None:
                self._resolve_ready(False)
                return
            self._stack = stack
            self._connected = True
            self._attach_reconnect()
            self._resolve_ready(True)
            while True:
                op = await self._ops.get()
                try:
                    if op.kind == "close":
                        await self._do_close()
                        self._settle(op.future, None)
                        break
                    if op.kind == "reconnect":
                        self._settle(
                            op.future,
                            await self._run_reconnect(op.tool_name, op.stale_tool),
                        )
                        if self._stack is None:
                            # 连接没能重建：本 owner 任务使命结束（一个 task 只服务一次
                            # 连接会话），由下一次 connect() 重新拉起并重试。
                            break
                finally:
                    # 指令一旦从队列取出就必须被收尾，否则调用方会永久等待：
                    # 重连是在工具调用 task 里 await 的，外层没有超时兜底。
                    self._settle(op.future, None)
        except asyncio.CancelledError:
            self._resolve_ready(False)
            raise
        except BaseException:
            # 必须用 BaseException：anyio task group 退出的异常形态不固定——
            # 组成员全是 Exception 时构造出的是 ExceptionGroup（*是* Exception
            # 子类），但只要含非 Exception 成员（如 CancelledError）就变成真正的
            # BaseExceptionGroup，Exception 兜不住。漏兜会让 owner task 静默死亡、
            # ready 永不置位，从而把 connect()（启动路径）或重连的调用方永久挂住。
            logger.exception("MCP server '{name}' owner task 异常退出", name=self.name)
            self._resolve_ready(False)
        finally:
            self._connected = False
            await self._do_close()
            self._drain_ops()

    async def _open(self) -> AsyncExitStack | None:
        """在本 task 内建立连接；失败或超时记日志并返回 None。"""
        from nanobee.agent.tools.mcp import connect_mcp_servers

        try:
            # 必须用 asyncio.timeout 而不是 asyncio.wait_for：wait_for 会把传入的
            # 协程包成新 task，cancel scope 就在**新 task** 里 enter，之后 owner task
            # 关闭它时又会变成「跨 task 退出 cancel scope」。asyncio.timeout 在当前
            # task 内原地取消，宿主 task 语义得以保持。
            async with asyncio.timeout(CONNECT_ATTEMPT_TIMEOUT_S):
                stacks = await connect_mcp_servers(
                    {self.name: self._cfg}, self._registry, default_cwd=self._default_cwd,
                )
        except TimeoutError:
            # 兜底上界：卡死的 server（如 stdio 子进程起不来）不能让 connect()
            # ——每条消息都会调用它——无限等待。
            logger.error(
                "MCP server '{name}' 连接超时（{timeout}s），本次尝试放弃",
                name=self.name,
                timeout=CONNECT_ATTEMPT_TIMEOUT_S,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 只记异常类名不记 str(exc)：httpx/anyio 的异常消息内嵌完整请求 URL
            # （query 携带 key）。脱敏责任在连接层（tools/mcp.py）——它已对日志中的
            # URL 做脱敏且不再输出原始 traceback，故上层不得再以「完整堆栈由连接
            # 层记录」为由把原始异常写进日志。
            logger.warning(
                "MCP server '{name}' 连接失败（冷却后重试）: {error}",
                name=self.name,
                error=type(exc).__name__,
            )
            return None
        return stacks.get(self.name)

    async def _do_close(self) -> bool:
        """拆解本 server 的连接（幂等），返回是否完成。

        Returns:
            True 表示拆解完成（或本来就没有连接）；False 表示拆解超时被放弃，
            连接可能未被完整回收（活跃 scope / 子进程泄漏到进程退出）。

        必须用 asyncio.timeout 原地取消而不是 wait_for：aclose 内部会退出
        cancel scope（enter 发生在本 owner task），wait_for 会把协程挪到新
        task，超时取消时 scope 就在错误的 task 里退出。
        """
        stack = self._stack
        self._stack = None
        self._connected = False
        if stack is None:
            return True
        try:
            async with asyncio.timeout(PER_SERVER_OP_TIMEOUT_S):
                await stack.aclose()
        except TimeoutError:
            logger.error(
                "MCP server '{name}' 拆解超时（{timeout}s）：中断拆解并放弃回收该连接"
                "（子进程可能残留至进程退出），owner 随后退出以便下次连接重建",
                name=self.name,
                timeout=PER_SERVER_OP_TIMEOUT_S,
            )
            return False
        except Exception:
            logger.exception(
                "MCP server '{name}' 关闭时出错：连接可能未被完整回收", name=self.name,
            )
        return True

    async def _run_reconnect(
        self, tool_name: str | None, stale_tool: Tool | None = None,
    ) -> Tool | None:
        """执行重连；单次失败只记日志并返回 None，不让 owner task 因此退出。"""
        try:
            return await self._do_reconnect(tool_name, stale_tool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server '{name}' 重连异常", name=self.name)
            return None

    async def _do_reconnect(
        self, tool_name: str | None, stale_tool: Tool | None = None,
    ) -> Tool | None:
        """在 owner task 内关闭旧连接并重连（同 task，故 scope 退出合法）。"""
        from nanobee.agent.tools.mcp import unregister_server_tools

        current_tool = self._registry.get(tool_name) if tool_name else None
        if current_tool is not None and current_tool is not stale_tool and self._connected:
            # 已有别的调用方完成了重连：直接交回新工具，避免再拆建一次
            return current_tool

        logger.warning(
            "MCP server '{name}' session terminated; refreshing connection", name=self.name,
        )
        unregister_server_tools(self._registry, self.name)
        if not await self._do_close():
            # 旧连接的拆解超时被放弃：本 owner 会话作废（一个 task 只服务一次
            # 连接会话），由下一次 connect() 重新拉起。带着半途退出的 scope
            # 继续 _open() 会在不干净的 anyio 状态之上叠连接，风险不可控。
            logger.error(
                "MCP server '{name}' 重连中止：旧连接拆解超时（下次 connect 重建）",
                name=self.name,
            )
            return None
        stack = await self._open()
        self._stack = stack
        self._connected = stack is not None
        if stack is None:
            logger.warning(
                "MCP server '{name}' reconnect failed after session termination", name=self.name,
            )
            return None
        self._attach_reconnect()
        if tool_name is None:
            return None
        return self._registry.get(tool_name)

    def _attach_reconnect(self) -> None:
        """为本 server 的 MCP Wrapper 注入重连回调（回投本 owner task）。"""
        from nanobee.agent.tools.mcp import attach_reconnect_handlers

        async def _reconnect(server_name: str, tool_name: str, stale_tool: Tool) -> Tool | None:
            return await self.request_reconnect(tool_name, stale_tool)

        attach_reconnect_handlers(self._registry, [self.name], _reconnect)

    def _resolve_ready(self, connected: bool) -> None:
        """置位连接结果，唤醒 ``start()`` 的等待者。"""
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_result(connected)

    @staticmethod
    def _settle(future: asyncio.Future, value: Any) -> None:
        """安全置位调用方等待的 future（可能已被超时取消）。"""
        if not future.done():
            future.set_result(value)

    def _drain_ops(self) -> None:
        """owner 退出后收尾队列中未处理的指令，避免调用方永久等待。"""
        while not self._ops.empty():
            self._settle(self._ops.get_nowait().future, None)


class MCPManager:
    """管理 MCP 服务器连接生命周期。

    职责：
    - 懒加载连接配置的 MCP 服务器（每 server 一个 owner task）
    - 管理连接状态（已连接/连接中）
    - 关闭所有 MCP 连接
    """

    def __init__(
        self,
        mcp_servers: dict | None = None,
        *,
        retry_cooldown_s: float | None = None,
    ) -> None:
        """初始化 MCP 管理器。

        Args:
            mcp_servers: MCP 服务器配置字典，key 为服务器名，value 为配置
            retry_cooldown_s: 连接失败后的重试冷却秒数；None 用模块默认
                RETRY_COOLDOWN_S，测试可传 0 关闭冷却。
        """
        self._servers: dict = mcp_servers or {}
        self._owners: dict[str, _ServerOwner] = {}
        # 各 server 最近一次连接失败的时刻（loop.time()）：冷却期判断的依据
        self._failed_at: dict[str, float] = {}
        self._retry_cooldown = RETRY_COOLDOWN_S if retry_cooldown_s is None else retry_cooldown_s

    @property
    def connected(self) -> bool:
        """是否已连接至少一个 MCP 服务器。"""
        return any(owner.connected for owner in self._owners.values())

    @property
    def has_servers(self) -> bool:
        """是否有配置的 MCP 服务器。"""
        return bool(self._servers)

    async def connect(self, tools: ToolRegistry, *, default_cwd: str | None = None) -> None:
        """懒加载连接配置的 MCP 服务器。

        幂等：已连接的 server 直接复用；正在连接的 server 会等待同一结果，
        不会出现「后来者直接返回、首轮缺失 MCP 工具」的竞态。
        单个 server 失败只记日志，不影响其它 server，也不抛出异常；失败后进入
        冷却期（RETRY_COOLDOWN_S），期间的消息不再重复付出完整连接尝试。

        Args:
            tools: 工具注册表，MCP 工具将注册到此注册表
            default_cwd: MCP stdio 进程的默认工作目录，未配置 cwd 时使用
                         通常传入 data_dir，避免文件导出到任意 CWD
        """
        if not self._servers:
            return

        now = asyncio.get_running_loop().time()
        owners: list[_ServerOwner] = []
        created = False
        for name, cfg in self._servers.items():
            owner = self._owners.get(name)
            if owner is None:
                failed_at = self._failed_at.get(name)
                if failed_at is not None and now - failed_at < self._retry_cooldown:
                    # 冷却期内：跳过新建（在途/已登记的 owner 不受影响）。
                    # 不记日志——kernel 每条消息都会走到这里，稳态必须零噪音。
                    continue
                owner = _ServerOwner(name, cfg, tools, default_cwd=default_cwd)
                self._owners[name] = owner
                created = True
            owners.append(owner)

        # 稳态快路径：kernel 每条消息都会调用 connect()，全部已连接时必须零开销、
        # 零日志。连接失败的 server 不留在登记里（但记有冷却时间戳），冷却结束后
        # 下轮 created=True，重试语义不变。
        if not created and all(owner.connected for owner in owners):
            return

        logger.info("MCP: 开始连接 {count} 个服务器", count=len(owners))
        results = await asyncio.gather(
            *(owner.start() for owner in owners), return_exceptions=True,
        )

        connected_count = 0
        for owner, result in zip(owners, results):
            if result is True:
                connected_count += 1
                self._failed_at.pop(owner.name, None)
                continue
            if isinstance(result, BaseException):
                logger.error(
                    "MCP server '{name}' 连接异常: {error}", name=owner.name, error=result,
                )
            # 只在 owner 已退出时摘除登记：仍在运行的 owner 可能正忙于重连或关闭，
            # 摘除会让下一次 connect() 另建一条逃逸回收的连接（连接与子进程泄漏）。
            if not owner.alive and self._owners.get(owner.name) is owner:
                del self._owners[owner.name]
                # 记录冷却起点：挂死型故障下，防止关停后的每条消息都重付完整尝试。
                self._failed_at[owner.name] = asyncio.get_running_loop().time()

        if connected_count:
            logger.info("MCP: 成功连接 {count} 个服务器", count=connected_count)
        else:
            logger.warning("MCP: 没有 MCP 服务器成功连接（冷却后自动重试）")

    async def close(self) -> None:
        """关闭所有 MCP 连接。

        幂等：多次调用安全。单个 server 关闭超时不影响其它 server。
        """
        owners = list(self._owners.values())
        # 幂等空跑（owners 为空）不打印，避免二次 close 刷噪音日志
        if owners:
            logger.info("MCP: 开始关闭 {count} 个服务器", count=len(owners))
        # 刻意不在此处清空登记：清空会让 close 期间到来的 connect() 建立一条
        # 逃过回收的新连接（连接与子进程泄漏）。登记在全部拆解完成后逐个摘除，
        # 期间到来的 connect() 会复用「正在关闭」的 owner，从而 fail-closed。
        async def _close_one(owner: _ServerOwner) -> bool:
            """关闭单个 owner：超时/异常只影响它自己，不影响其它 server。"""
            try:
                await asyncio.wait_for(
                    owner.request_close(), timeout=CLOSE_WAIT_TIMEOUT_S,
                )
            except TimeoutError:
                logger.error(
                    "MCP server '{name}' 关闭超时（{timeout}s），已放弃等待该连接",
                    name=owner.name,
                    timeout=CLOSE_WAIT_TIMEOUT_S,
                )
                return False
            except Exception:
                logger.exception("MCP server '{name}' 关闭异常", name=owner.name)
                return False
            logger.info("MCP server '{name}': 已关闭", name=owner.name)
            return True

        # 各 owner 相互独立（每 server 一个 task 正是本设计的核心收益），故并行关闭：
        # 总耗时收敛到「单 server 上限」，而不是随 server 数线性放大（N × 7s）。
        results = await asyncio.gather(*(_close_one(owner) for owner in owners))
        closed_count = sum(1 for ok in results if ok)
        if owners:
            logger.info(
                "MCP: 关闭完成（成功 {closed}/{total}）", closed=closed_count, total=len(owners),
            )

        for owner in owners:
            # 只摘除已退出的 owner：仍存活的 owner 可能正忙于建连或重连（其内部
            # 执行都受机制上界约束），摘除后它只被自己的 pending task 引用（连接/
            # 子进程/task 三重泄漏），而且下一次 connect() 会另建一条连接，正好
            # 破坏上面注释承诺的 fail-closed。
            if not owner.alive and self._owners.get(owner.name) is owner:
                del self._owners[owner.name]
                # 一并注销该 server 的 MCP 工具：死会话的 wrapper 对 LLM 仍可见，
                # 保留只会诱导反复调用必然失败的工具。
                from nanobee.agent.tools.mcp import unregister_server_tools

                unregister_server_tools(owner._registry, owner.name)
