# 插件开发指导

Nanobee 的插件体系遵循**框架无知论**（Framework Ignorance Principle）——框架只提供注册、调度、隔离机制，所有策略决策（什么值得记、什么时候触发、段标题叫什么）由 LLM 或插件自主完成。

---

## 目录

- [插件体系概述](#插件体系概述)
- [内置插件 vs 实例级插件](#内置插件-vs-实例级插件)
- [插件类型](#插件类型)
- [快速开始：最简插件](#快速开始最简插件)
- [插件生命周期](#插件生命周期)
- [Hook 机制（PluginHookMixin）](#hook-机制pluginhookmixin)
  - [Prompt 注入（contribute_to_prompt）](#prompt-注入contribute_to_prompt)
  - [工具过滤（contribute_to_tools）](#工具过滤contribute_to_tools)
  - [工具前后拦截（on_pre_invoke / on_post_invoke）](#工具前后拦截on_pre_invoke--on_post_invoke)
  - [消息完成回调（on_message_completed）](#消息完成回调on_message_completed)
  - [事件系统（EventBus）与迁移指南](#事件系统eventbus与迁移指南)
- [开发 Tool 插件](#开发-tool-插件)
  - [简单工具插件](#简单工具插件)
  - [路径安全与沙箱](#路径安全与沙箱)
- [开发 Memory 插件](#开发-memory-插件)
- [开发 Channel 插件](#开发-channel-插件)
- [开发 Audit 插件](#开发-audit-插件)
- [插件配置隔离](#插件配置隔离)
- [插件临时目录（tmp 注入）](#插件临时目录tmp-注入)
- [插件元数据（plugin.toml）](#插件元数据plugintoml)
- [命名规范](#命名规范)
- [测试指南](#测试指南)
- [最佳实践](#最佳实践)

---

## 插件体系概述

```
┌──────────────────────────────────────────┐
│              PluginManager               │
│  扫描 → 加载 → 依赖排序 → 注册          │
├──────────────────────────────────────────┤
│                                          │
│  NanobeePlugin (基类)                    │
│    ├── ToolPlugin    — 工具调用          │
│    ├── MemoryPlugin  — 记忆存储底座接口  │
│    ├── ChannelPlugin — 通信渠道          │
│    └── Audit         — 纯监听型          │
│                                          │
│  PluginHookMixin (生命周期的钩子)         │
│    ├── contribute_to_prompt              │
│    ├── contribute_to_tools               │
│    ├── on_pre_invoke                     │
│    ├── on_post_invoke                    │
│    └── on_message_completed              │
└──────────────────────────────────────────┘
```

插件独立打包为目录，有两种部署位置：

| 类型 | 路径 | 说明 |
|------|------|------|
| **内置插件** | `<package>/builtin/` | 框架自带，始终自动加载，通过 `blacklist` 禁用 |
| **实例级插件** | `<data-dir>/plugins/` | 跟随实例，自动发现（无需配置），与实例技能同机制 |

---

## 内置插件 vs 实例级插件

框架遵循**框架无知论**——不强制用户配置插件路径。两种插件都从固定目录自动发现：

```
nanobee/                              # 框架包
├── builtin/                          # 内置插件（打包分发）
│   ├── tool_fs/
│   ├── tool_shell/
│   └── ...

<data-dir>/                           # 实例根目录（如 ~/.nanobee/ 或 /nanobee-data/<instance>/）
├── config.yaml                       # 实例配置（不再需要 plugin_dirs 字段）
├── plugins/                          # 实例级插件（自动发现）
│   └── <plugin-name>/
│       ├── plugin.toml
│       ├── plugin.py
│       ├── tests/                    # 测试放在插件目录内
│       └── ...                       # 同级模块可通过 from .xxx import 相对导入
├── skills/                           # 实例技能（与插件同机制）
└── users/
```

**关键规则：**

1. **实例插件自动发现**：`<data-dir>/plugins/` 目录存在即自动扫描加载，无需在 `config.yaml` 中配置 `plugin_dirs`
2. **`plugin_dirs` 仅用于覆盖**：当插件放在非默认路径时，通过 `plugin_dirs: [<custom-path>]` 指定
3. **相对导入原生可用**：PluginManager 为每个插件创建独立命名空间包，`from .xxx import yyy` 正常工作，不污染全局 `sys.path`
4. **通过 `blacklist` 禁用**：`agents.defaults.blacklist: [<plugin-name>]` 可禁用内置或实例插件

**实例级插件示例：**

```yaml
# <data-dir>/config.yaml
data_dir: "<data-dir>"
# plugin_dirs 不需要！实例插件从 <data-dir>/plugins/ 自动发现
agents:
  defaults:
    blacklist:
      - <builtin-plugin-to-disable>     # 禁用某个内置插件
```

```
<data-dir>/plugins/
└── <plugin-name>/
    ├── plugin.toml       # name 字段定义插件名
    ├── plugin.py         # 主模块
    ├── __init__.py       # 可选，通常为空
    ├── helper.py         # 同级模块，用 from .helper import xxx 导入
    └── tests/            # 测试（不属于框架项目，实例自行维护）
        ├── conftest.py   # 创建临时 Kernel，通过 PluginManager 加载本插件
        └── test_xxx.py
```

**实例级插件测试指引：**

实例插件的测试**不应**写在 nanobee 框架项目的 `tests/` 中。测试文件放在插件自己的 `tests/` 目录下，通过 PluginManager 加载：

```python
# <data-dir>/plugins/<plugin-name>/tests/conftest.py
from pathlib import Path
import pytest
from nanobee.kernel import NanobeeKernel
from nanobee.kernel.core_parser import CoreMDParser

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # 即 <plugin-name>/ 目录


@pytest.fixture
async def kernel(tmp_path):
    """创建临时 Kernel，通过 PluginManager 加载本插件。"""
    CoreMDParser.create_default(tmp_path / "core.md")
    config = {
        "data_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
        "plugin_dirs": ["__replace__", str(PLUGIN_DIR)],  # 只加载本插件
    }
    k = NanobeeKernel(config=config)
    await k.boot()
    yield k
    await k.shutdown()


@pytest.fixture
async def plugin(kernel):
    """获取已加载的插件实例。"""
    return kernel.plugin_manager.get("<plugin-name>")
```

```python
# <data-dir>/plugins/<plugin-name>/tests/test_csv.py
@pytest.mark.asyncio
async def test_parse_csv(plugin):
    result = plugin._pipeline_parse_csv("name,age\nAlice,30", "test")
    assert result["ok"]
```

**`plugin.toml` 中 `name` 字段示例：**

```toml
[plugin]
name = "<plugin-name>"
version = "0.1.0"
description = "<插件描述>"
type = "tool"

[config]
enabled = true
```

***

## 插件类型

| 类型 | 基类 | `plugin_type` | 说明 |
|------|------|---------------|------|
| **Tool** | `ToolPlugin` | `"tool"` | 注册工具供 LLM 调用（文件、Shell、Web 等） |
| **Memory** | `MemoryPlugin` | `"memory"` | 记忆存储底座接口（store/retrieve），框架无内置实现 |
| **Channel** | `NanobeePlugin` | `"channel"` | 通信渠道（CLI、HTTP、钉钉等），接收外部消息注入 Agent |
| **Audit** | `NanobeePlugin` | `"audit"` | 纯监听型插件，不贡献工具也不注入 Prompt，仅监听事件 |

> 注意：`ToolPlugin` 需通过 `nanobee.plugins.tool.ToolPlugin` 导入（不在 `nanobee.plugins.__init__` 导出）。

---

## 快速开始：最简插件

### 文件结构

```
my_plugin/
├── plugin.py        # 插件实现（必须）
├── plugin.toml      # 插件元数据（必须）
├── __init__.py      # 空文件（可选，使目录为 Python 包）
└── README.md        # 说明文档（可选）
```

### plugin.toml

```toml
[plugin]
name = "my_plugin"
version = "0.1.0"
description = "一句话描述插件功能"
author = "your-name"
type = "tool"          # tool / memory / channel / audit
```

### plugin.py

```python
from nanobee.plugins.tool import ToolPlugin


class MyToolPlugin(ToolPlugin):
    name = "my_plugin"
    plugin_type = "tool"

    def get_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "my_tool",
                    "description": "举个栗子工具",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "输入内容",
                            },
                        },
                        "required": ["input"],
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name == "my_tool":
            return f"你输入了: {kwargs['input']}"
        raise ValueError(f"未知工具: {tool_name}")
```

---

## 插件生命周期

```
PluginManager.scan()  ──▶  发现 plugin.toml，解析为 PluginDescriptor
                          │
PluginManager.load()  ──▶  动态导入模块
     │                     │
     ▼                     ▼
initialize(kernel)  ──▶  注入内核引用，初始化资源
     │
     ▼
on_load()            ──▶  加载完成后的回调（如注册 Hook）
     │
     ▼
on_enable()          ──▶  插件启用时调用
     │
     ▼
  [运行中]           ──▶  调用生命周期 Hook
     │
     ▼
on_disable()         ──▶  插件禁用时调用
     │
     ▼
on_unload()          ──▶  卸载时清理
     │
     ▼
destroy()            ──▶  彻底销毁
```

### 生命周期方法

```python
class MyPlugin(NanobeePlugin):
    name = "my_plugin"
    plugin_type = "audit"

    async def initialize(self, kernel: "Kernel") -> None:
        """插件加载时初始化（必须调用 super()）"""
        await super().initialize(kernel)
        # 这里可以缓存 kernel 引用
        self._kernel = kernel

    async def on_load(self) -> None:
        """插件加载完成后的回调"""
        logger.info("{} 插件已加载", self.name)

    async def on_enable(self) -> None:
        """插件启用时的回调"""
        pass

    async def on_disable(self) -> None:
        """插件禁用时的回调"""
        pass

    async def on_unload(self) -> None:
        """卸载时清理资源（文件句柄、网络连接等）"""
        pass

    async def destroy(self) -> None:
        """彻底销毁，释放所有资源"""
        pass
```

---

## Hook 机制（PluginHookMixin）

所有插件自动继承 `PluginHookMixin`，可按需覆盖以下 Hook。

### Prompt 注入（contribute_to_prompt）

向 System Prompt 的指定段注入文本。框架按 `plugin.plugin_type`（或更优先的 `plugin.stage`）自动生成段标题。

```python
def contribute_to_prompt(self, context: PromptBuildContext) -> str | None:
    """向 System Prompt 注入文本。

    返回的字符串会被放入以 plugin_type 为标题的段中。
    若返回 None，此插件不贡献任何 prompt 内容。
    """
    return "这里是一些上下文知识..."
```

段标题由 `context_pipeline._map_plugin_stage()` 决定：

```
优先级: plugin.stage > plugin.plugin_type > metadata.plugin_type
```

框架不做语义翻译，`plugin_type="memory"` 的段标题就是 `## memory`。

### 工具过滤（contribute_to_tools）

动态增删 LLM 可用的工具列表：

```python
def contribute_to_tools(
    self,
    context: PromptBuildContext,
    current_tool_names: list[str],
) -> list[str]:
    """动态过滤可用工具。

    返回保留的工具名称列表。返回空列表表示禁用所有工具。
    返回 None 或原始列表表示不做任何修改。
    """
    # 示例：禁止使用危险工具
    forbidden = {"execute_shell", "danger_tool"}
    return [name for name in current_tool_names if name not in forbidden]
```

### 工具前后拦截（on_pre_invoke / on_post_invoke）

在工具执行前后插入逻辑——鉴权、参数修改、结果修改、副作用：

```python
async def on_pre_invoke(
    self,
    context: PromptBuildContext,
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """工具执行前调用。

    可修改参数，或抛异常阻止工具执行。
    """
    if tool_name == "danger_tool":
        raise ValueError("危险工具被禁用")
    # 确保所有写操作包含必要参数
    if tool_name == "write_file" and "content" not in args:
        raise ValueError("write_file 必须包含 content 参数")
    return args  # 可返回修改后的参数 dict


async def on_post_invoke(
    self,
    context: PromptBuildContext,
    tool_name: str,
    result: Any,
) -> Any:
    """工具执行后调用。

    可修改结果，或记录副作用。
    """
    logger.info("工具 {tool_name} 执行完毕, 结果长度: {len(str(result))}")
    return result  # 可返回修改后的结果
```

### 消息完成回调（on_message_completed）

对话轮次结束后调用（后台执行，不阻塞 LLM 响应）。插件的调度行为由 ``plugin.toml`` 中的
``[hooks.on_message_completed]`` 段控制（参见 [Hook 调度元数据](#hook-调度元数据声明)）。

```python
async def on_message_completed(
    self,
    context: PromptBuildContext,
    messages: list[dict[str, Any]],
) -> None:
    """一轮对话结束后调用。

    适用于审计日志、后台整理等操作。
    此 Hook 的执行不会阻塞 LLM 响应返回给用户。
    是否需要阻塞下一轮 dispatch 由 plugin.toml 中的 block_next 声明控制。
    """
    logger.info("对话轮次结束, 消息数: {len(messages)}")
```

---

### Hook 调度元数据声明

插件可通过 ``plugin.toml`` 的 ``[hooks.<hook_name>]`` 段声明各 Hook 的调度策略。
框架只读标记、不懂含义（FIP），按声明驱动调度。

支持的 Hook 名：``on_message_completed``、``on_pre_invoke``、``on_post_invoke``。

#### 字段说明

| 字段 | 类型 | 默认值 | 适用 Hook | 说明 |
|------|------|--------|-----------|------|
| ``block_next`` | bool | ``false`` | 仅 ``on_message_completed`` | 是否阻塞同 ``context_id`` 的下一次 dispatch |
| ``priority`` | int | ``10`` | 全部 | 同组 Hook 的执行优先级，数值越大越先执行 |
| ``timeout`` | float | ``0.0`` | ``on_message_completed``（block_next=true 时） | 阻塞型 Hook 的超时时间（秒），``0`` 表示不设超时 |

#### 调度语义

**``on_message_completed``（后台 fire-and-forget 模式）：**

```
block_next=true  → 框架在下一次 dispatch 前 await 本 Hook 完成
                   （适用于 memory 存储等必须完成的操作）

block_next=false → 框架 fire-and-forget，不等待、不追踪
                   （适用于 audit 日志等丢了也无所谓的操作）

priority         → block_next 同组内按降序执行
                   （如 priority=100 的插件在 priority=50 之前执行）

timeout          → 仅 block_next=true 时生效，超时跳过该 Hook 继续处理下一个
```

**``on_pre_invoke`` / ``on_post_invoke``（同步拦截器链模式）：**

```
priority         → 拦截器链中按降序执行
                   （如 security 检查 priority=100 先于 logging priority=10 执行）

block_next       → 不适用（拦截器为同步内联调用，不存在"下一次 dispatch"的概念）

timeout          → 暂不支持（拦截器在工具执行流中内联调用，按异常隔离兜底）
```

#### 声明示例

**非阻塞型（audit 日志）**：
```toml
# plugin.toml
[hooks.on_message_completed]
block_next = false      # 不阻塞下一步，丢了也无所谓
priority = 10           # 低优先级
```

**阻塞型（memory 存储）**：
```toml
# plugin.toml
[hooks.on_message_completed]
block_next = true       # 必须存完才能处理下一条消息
priority = 80           # 高优先级，先于其他 Hook 执行
```

**混合声明**：
```toml
[hooks.on_message_completed]
block_next = true
priority = 80
timeout = 5.0

[hooks.on_pre_invoke]
priority = 100
# 注：on_pre_invoke 为同步拦截器，仅 priority 参与排序；
#     block_next / timeout 不适用于此 Hook
```

#### 向后兼容

- 不声明 ``[hooks]`` 段的插件 → 默认 ``priority=10``（``on_message_completed`` 额外默认 ``block_next=false, timeout=0.0``）
- 即现有插件无需修改即可正常工作，行为与改造前一致

---

### 事件系统（EventBus）与迁移指南

框架通过 ``event_bus`` 发布内部事件，插件可通过 ``kernel.event_bus.subscribe()`` 订阅。
以下为当前活跃的事件列表：

| 事件 | 载荷 | 说明 |
|------|------|------|
| ``agent.iteration_start`` | ``context_id``, ``turn_id`` | 每个 Agent turn 开始时触发 |
| ``agent.turn_saved`` | ``context_id``, ``turn_id``, ``latency_ms``, ``tools_used`` | SAVE 状态：对话历史持久化完成后触发 |
| ``agent.outbound`` | ``channel``, ``chat_id``, ``content``, ``metadata`` | Agent 回复组装完成后发送到通道 |
| ``subagent.spawned`` | ``task_id``, ``label``, ``task`` | 子代理启动时触发（通知通道立即发送用户可见消息） |

#### 破坏性变更：`agent.turn_completed` 已移除（2026-06-27）

原 ``agent.turn_completed`` 事件在 LLM 响应完成后发布，载荷包含：

```python
{
    "context_id": "...",
    "final_content": "...",
    "stop_reason": "completed",
    "tools_used": ["read_file", "execute_shell"],
    "usage": {"prompt_tokens": 100, "completion_tokens": 200},
}
```

**移除原因：** 该事件与 FIP 调度器 ``on_message_completed`` Hook 构成双重通知路径，造成隐式竞争。
框架统一到 Hook 机制：插件通过 ``plugin.toml`` 声明调度策略（``block_next`` / ``priority`` / ``timeout``），
框架只读标记、按声明驱动，符合 FIP。

**迁移路径：**

| 旧方案（已移除） | 新方案（推荐） |
|---|---|
| ``event_bus.subscribe("agent.turn_completed", handler)`` | 实现 ``on_message_completed()`` Hook + 在 ``plugin.toml`` 声明 ``[hooks.on_message_completed]`` |
| 事件载荷 ``final_content`` / ``stop_reason`` | 通过 ``messages`` 参数自行提取最后一轮 assistant 消息 |
| 事件载荷 ``tools_used`` | 统计 ``messages`` 中 ``role=tool`` 的消息 |
| 事件载荷 ``usage``（token 用量） | 当前 Hook 未传递 usage（如需，在 Hook 签名中扩展） |

**迁移示例：**

```python
# 旧方案（已不可用）
class MyOldPlugin(NanobeePlugin):
    async def initialize(self, kernel):
        await super().initialize(kernel)
        kernel.event_bus.subscribe("agent.turn_completed", self._on_turn_done)

    async def _on_turn_done(self, data: dict):
        ctx_id = data["context_id"]
        tools = data["tools_used"]
        # ...

# 新方案
class MyNewPlugin(NanobeePlugin):
    name = "audit_logger"
    plugin_type = "audit"

    async def on_message_completed(self, context, messages):
        ctx_id = context.context_id
        tools = [
            m.get("name") or m.get("function", {}).get("name", "?")
            for m in messages
            if m.get("role") == "tool"
        ]
        # ...
```

```toml
# plugin.toml
[hooks.on_message_completed]
block_next = false
priority = 10
```

> 如需获取 token 用量统计，请通过 ``AgentRunner`` 的返回结果自行跟踪，或在 Hook 层面扩展载荷字段。
> ``agent.turn_saved`` 事件仍然可用（在 SAVE 状态触发），但其载荷不含 ``final_content`` 和 ``usage``。

---

## 开发 Tool 插件

### 简单工具插件

参考 `nanobee/builtin/tool_echo/plugin.py`：

```python
from nanobee.plugins.tool import ToolPlugin


class ToolEchoPlugin(ToolPlugin):
    name = "tool_echo"
    plugin_type = "tool"

    def get_tools(self) -> list[dict]:
        """返回 OpenAI Function Calling 格式的工具定义列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "回显输入文本（测试用）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要回显的文本",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
        ]

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name == "echo":
            return f"[echo] {kwargs.get('text', '')}"
        raise ValueError(f"未知工具: {tool_name}")
```

### 路径安全与沙箱

操作文件系统的工具需要路径安全校验。框架提供了两种模式，按需选用。

#### 模式一：自带沙箱隔离（推荐给文件/Shell 工具）

框架在 `NanobeePlugin` 基类上提供了 `resolve_path()` 公开方法，封装了沙箱边界校验。
实例级插件**无需**导入 `nanobee.kernel` 内部模块，通过基类方法即可获得完整保护：

```python
from pathlib import Path
from nanobee.plugins.tool import ToolPlugin


class ToolFsPlugin(ToolPlugin):
    name = "tool_fs"
    plugin_type = "tool"

    def _resolve_file_path(self, path_str: str) -> Path:
        """安全解析文件路径。

        通过基类 resolve_path() 获得沙箱保护：
        - 有沙箱时走沙箱边界校验，越界抛 SandboxViolationError
        - 无沙箱时回退到 Path.resolve()
        """
        return self.resolve_path(path_str)

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name == "read_file":
            path = kwargs["path"]
            safe_path = self._resolve_file_path(path)
            content = safe_path.read_text("utf-8")
            return content
        raise ValueError(f"未知工具: {tool_name}")
```

**`resolve_path()` 签名：**

```python
def resolve_path(self, path_str: str, *, for_write: bool = False) -> Path:
```

- `path_str`：文件路径（相对基于 context_root 解析，绝对直接使用）
- `for_write`：是否为写操作（写操作有更严格的路径校验）
- 有沙箱时调用 `sandbox.resolve_with_fallback()` 或 `resolve_safe_writable()`
- 无沙箱时回退到 `Path.resolve()`

#### 模式二：仅校验绝对路径（推荐给数据搬运工具）

不需要沙箱隔离的工具（如数据导入/导出），直接校验绝对路径即可，
不引入沙箱依赖。参考 `tool_dingtalk` 的实现：

```python
from pathlib import Path
from nanobee.plugins.tool import ToolPlugin


class MyDataTool(ToolPlugin):
    name = "my_data_tool"
    plugin_type = "tool"

    def _resolve_input_dir(self, path_str: str) -> Path:
        """解析输入目录（仅接受绝对路径）。

        不接受相对路径——多租户沙箱隔离下相对路径不可靠。
        路径不存在或不是目录时直接报错并引导使用绝对路径。
        """
        path = Path(path_str).resolve()
        if not path.is_dir():
            raise ValueError(
                f"输入目录不存在或不是目录: {path_str}，请使用绝对路径"
            )
        return path

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name == "import_data":
            input_dir = self._resolve_input_dir(kwargs["input_dir"])
            # 扫描目录下的数据文件
            csv_files = list(input_dir.glob("*.csv"))
            if not csv_files:
                return f"错误：{input_dir} 中未找到 .csv 文件"
            # ... 处理数据
            return f"已导入 {len(csv_files)} 个文件"
        raise ValueError(f"未知工具: {tool_name}")
```

**两种模式对比：**

| | 模式一 `self.resolve_path()` | 模式二 绝对路径校验 |
|---|---|---|
| 适用场景 | 需沙箱隔离的文件/Shell 工具 | 数据搬运/导入导出工具 |
| 沙箱依赖 | 有沙箱自动生效 | 不依赖沙箱 |
| 相对路径 | 基于 context_root 解析 | 不接受（报错引导） |
| 路径校验 | 沙箱边界 + 存在性自查 | 仅查存在性 |
| 导入方式 | 基类公开 API | 纯标准库 |

---

## 开发 Memory 插件

框架遵循**框架无知论**——不决定什么值得记、什么时候触发。记忆插件仅作为存储底座，框架在 COMPACT 状态时调用 `store()`，在 BUILD 状态调用 `retrieve()`。

```python
from nanobee.plugins.memory import MemoryPlugin


class MyMemoryPlugin(MemoryPlugin):
    name = "my_memory"
    plugin_type = "memory"

    async def store(
        self,
        messages: list[dict[str, Any]],
        user_context: "UserContext",
    ) -> None:
        """存储/提取记忆。

        在 Agent Loop COMPACT 状态触发。messages 是整个历史消息列表，
        插件可自行决定哪些内容值得存储。
        """
        # 提取重要的对话内容
        facts = self._extract_facts(messages)
        # 写入自己的记忆存储（文件、数据库、向量库等）
        await self._save_to_db(user_context.user_id, facts)

    async def retrieve(
        self,
        query: str,
        user_context: "UserContext",
        top_k: int = 5,
    ) -> str | None:
        """检索相关记忆。

        在 Agent Loop BUILD 状态触发。返回的字符串会被注入 System Prompt。
        """
        results = await self._query_db(user_context.user_id, query, top_k)
        if not results:
            return None
        return "\n".join(results)
```

> 框架当前无内置 Memory 插件。`_memory` 技能通过 LLM 自主管理 `memory/facts.md` 文件实现了最小化记忆。如需高级记忆策略（向量检索、语义聚类），请实现 MemoryPlugin。

---

## 开发 Channel 插件

Channel 插件负责通信渠道接入，将外部消息转化为内部 `InboundMessage` 注入 Agent。

```python
from nanobee.plugins.base import NanobeePlugin


class MyChannelPlugin(NanobeePlugin):
    name = "my_channel"
    plugin_type = "channel"

    async def initialize(self, kernel) -> None:
        await super().initialize(kernel)
        # 建立网络监听
        self._server = await self._start_server()

    async def on_unload(self) -> None:
        # 关闭网络连接
        if self._server:
            await self._server.close()

    async def _start_server(self):
        """启动自己的通信服务。"""
        # ...
```

典型实现参考 `nanobee/builtin/channel_cli/`、`nanobee/builtin/channel_http/`、`nanobee/builtin/channel_dingtalk/`。

---

## 开发 Audit 插件

Audit 是纯监听型插件，不贡献 prompt、不注册工具，仅通过 `on_message_completed` Hook 在对话结束时执行后台操作。

参考 `nanobee/builtin/audit_logger/plugin.py`：

```python
from nanobee.plugins.base import NanobeePlugin


class AuditLoggerPlugin(NanobeePlugin):
    name = "audit_logger"
    plugin_type = "audit"

    async def on_message_completed(
        self,
        context: PromptBuildContext,
        messages: list[dict[str, Any]],
    ) -> None:
        """记录每轮对话的审计日志。"""
        # 统计工具调用次数
        tool_calls = sum(1 for m in messages if m.get("role") == "assistant"
                         and "tool_calls" in m)
        # 写入日志
        logger.info(
            "Audit [{}]: {} 条消息, {} 次工具调用",
            context.context_id, len(messages), tool_calls,
        )
```

---

## 插件配置隔离

插件配置通过 `nanobee.yaml` 的 `plugins.<plugin_name>` 段注入，框架自动隔离前缀。

```yaml
# nanobee.yaml
plugins:
  my_plugin:
    enabled: true
    api_key: "sk-xxx"
    max_retries: 3
```

插件内部通过 `get_config()` 方法读取，自动剥离 `plugins.<plugin_name>` 前缀：

```python
class MyPlugin(NanobeePlugin):
    async def initialize(self, kernel) -> None:
        await super().initialize(kernel)
        # 读取 my_plugin.api_key, my_plugin.max_retries
        api_key = self.get_config("api_key", "default_key")
        max_retries = self.get_config("max_retries", 5)
        is_enabled = self.is_enabled()
```

---

## 插件临时目录（tmp 注入）

框架在 `ConversationContext` 中创建 `.tmp/` 目录，通过 ContextVar 按请求注入到插件：

```python
class MyPlugin(NanobeePlugin):
    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        # self.tmp 返回 <context_root>/.tmp/<plugin_name>/
        # 当前请求未绑定 ContextVar 时返回 None
        if self.tmp:
            tmp_file = self.tmp / "temp_data.txt"
            async with aiofiles.open(tmp_file, "w") as f:
                await f.write("临时数据")
            return f"写入临时文件: {tmp_file}"
        return "无临时目录可用"
```

---

## 插件元数据（plugin.toml）

```toml
[plugin]
name = "my_plugin"                     # 插件名称，必须唯一
version = "1.0.0"                      # 语义化版本
description = "一句话描述插件功能"       # 简短描述
author = "author-name"                 # 作者
type = "tool"                          # 插件类型: tool / memory / channel / audit
permissions = { network = true }      # 可选，声明需要的权限

[plugin.dependencies]
requires = ["tool_web"]               # 可选，依赖的其他插件

[config]
enabled = true                         # 默认启用

[hooks.on_message_completed]           # Hook 调度元数据（可选，不声明用默认值）
block_next = false                      # 是否阻塞下一轮 dispatch
priority = 10                           # 调度优先级（越大越优先）
```

**`[plugin].type` 字段说明**：

- TOML 中使用 `type` 字段声明插件类型（内部存储为 `plugin_type` 元数据属性）
- Python 类中使用 `plugin_type` 类变量（TOML 未提供 `type` 时作为回退值）
- 有效值: `tool` / `memory` / `channel` / `audit`

---

## 命名规范

- **目录名**：PEP 8 规范全小写 + 下划线，如 `tool_my_tool`、`channel_telegram`
- **类型前缀**（推荐）：`tool_` / `channel_` / `memory_` / `audit_`
- **Python 类名**：`PascalCase`，如 `ToolMyTool`、`ChannelTelegram`
- **plugin.toml name**：与目录名一致，如 `"tool_my_tool"`
- **禁止**：连字符（`-`），违反 PEP 8 模块命名规范

---

## 测试指南

### 单元测试

```python
import pytest
from nanobee.plugins.tool import ToolPlugin


class TestMyPlugin:
    def test_get_tools(self):
        plugin = MyPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "my_tool"

    @pytest.mark.asyncio
    async def test_execute_tool(self):
        plugin = MyPlugin()
        result = await plugin.execute_tool("my_tool", input="hello")
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        plugin = MyPlugin()
        with pytest.raises(ValueError, match="未知工具"):
            await plugin.execute_tool("nonexistent")
```

### 全量测试

```bash
# 激活虚拟环境后
source .venv/bin/activate

# 运行全部测试
python -m pytest tests/

# 运行特定插件测试
python -m pytest tests/test_tool_fs.py -v
```

---

## 最佳实践

1. **最小权限**：插件只覆盖需要的 Hook，不实现空方法
2. **错误隔离**：工具失败返回错误字符串而非抛异常（LLM 可读可恢复）
3. **异步优先**：所有 IO 操作使用 `async/await`，禁止同步阻塞
4. **按需沙箱**：需沙箱隔离的工具用 `self.resolve_path()`，数据搬运类工具直接校验绝对路径即可，不引入不必要的依赖
5. **日志代替 print**：使用 `logger = get_logger(__name__)`，配置在 `plugin.py` 中
6. **配置敏感信息**：API Key 等敏感信息通过 `nanobee.yaml` 配置读取，不硬编码
7. **临时文件**：使用 `self.tmp` 目录（框架自动清理），不使用系统临时目录
8. **等幂设计**：`on_load` / `on_enable` 可重复调用而不产生副作用
9. **类型注解**：所有公开方法必须提供完整类型注解
10. **框架无知**：插件不假设框架做了任何智能决策——框架只做机制，策略由 LLM 或插件自主决定
11. **Tool 定义遵循 OpenAI Function Calling 格式**：包含 `name`、`description`、`parameters`，描述须清晰准确以便 LLM 理解
