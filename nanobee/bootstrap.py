"""Nanobee Bootstrap — 组合根。

将配置加载、组件创建、AgentLoop 装配集中于此，
消除 kernel.py 对 agent/ 和 providers/ 的直接导入依赖。

CLI (run/gateway) 通过 bootstrap() 一行启动，
不再直接调用 kernel.boot_with_provider()。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobee.agent.loop import AgentLoop
from nanobee.config.loader import load_config, resolve_config_env_vars
from nanobee.config.schema import Config
from nanobee.kernel.kernel import NanobeeKernel
from nanobee.providers.factory import make_provider
from nanobee.utils.logger import logger


async def bootstrap(
    config_path: str | Path | None = None,
    *,
    config: Config | None = None,
    model: str | None = None,
    plugin_dirs: list[str] | None = None,
    start_services: bool = False,
    data_dir: str | None = None,
    **extra: Any,
) -> NanobeeKernel:
    """组合根：配置 → 组件创建 → AgentLoop 装配 → 启动。

    统一入口，替代 kernel.boot_with_provider()。
    优先使用 ``config`` 参数（已加载的 Config），
    否则从 ``config_path`` 加载。

    Args:
        config_path: 配置文件路径
        config: 已加载的 Config 对象（优先于 config_path）
        model: 模型名称（可选，覆盖配置）
        plugin_dirs: 插件目录列表（可选，覆盖配置）
        start_services: 是否启动后台服务（通道）
        data_dir: 数据目录（可选，覆盖配置）
        **extra: 传递给 AgentLoop.from_kernel() 的额外参数

    Returns:
        已启动的 NanobeeKernel 实例
    """
    # 1. 确定配置
    if config is not None:
        cfg = resolve_config_env_vars(config)
    elif config_path is not None:
        cfg = resolve_config_env_vars(load_config(Path(config_path)))
    else:
        cfg = Config()

    if data_dir is not None:
        cfg.data_dir = data_dir

    actual_model = model or cfg.agents.defaults.model

    # 2. 创建内核（仅构造，不启动）
    kernel = NanobeeKernel(config=cfg, plugin_dirs=plugin_dirs)

    # 3. 创建 LLM Provider
    provider = make_provider(cfg, model=actual_model)

    # 4. 装配 AgentLoop（从内核子组件创建）
    agent_loop = AgentLoop.from_kernel(
        provider=provider,
        workspace=kernel.data_dir,
        context_manager=kernel.context_manager,
        context_pipeline=kernel.context_pipeline,
        session_manager=kernel.session_manager,
        event_bus=kernel.event_bus,
        plugin_manager=kernel.plugin_manager,
        skill_manager=kernel.skill_manager,
        router=kernel.router,
        config=cfg,
        model=actual_model,
        message_injector=kernel.inject_message,
        **extra,
    )

    # 5. 将 AgentLoop 注入内核（必须在 boot() 之前，
    #    因为 boot() 会调用 agent.register_plugin_tools()）
    kernel.set_agent_loop(agent_loop)

    # 6. 启动内核（灵魂校验 + 插件加载 + 工具注册）
    await kernel.boot()

    # 7. 可选：启动后台服务（Gateway 模式）
    if start_services:
        await kernel.boot_services()

    logger.info("Nanobee 启动完成 (model={}, plugin_dirs={})", actual_model, plugin_dirs)

    return kernel
