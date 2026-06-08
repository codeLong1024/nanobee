# 插件开发指导

Nanobee 的插件体系遵循**框架无知论**（Framework Ignorance Principle）——框架只提供注册、调度、隔离机制，所有策略决策（什么值得记、什么时候触发、段标题叫什么）由 LLM 或插件自主完成。

---

## 目录

- [插件体系概述](#插件体系概述)
- [插件类型](#插件类型)
- [快速开始：最简插件](#快速开始最简插件)
- [插件生命周期](#插件生命周期)
- [Hook 机制（PluginHookMixin）](#hook-机制pluginhookmixin)
  - [Prompt 注入（contribute_to_prompt）](#prompt-注入contribute_to_prompt)
  - [工具过滤（contribute_to_tools）](#工具过滤contribute_to_tools)
  - [工具前后拦截（on_pre_invoke / on_post_invoke）](#工具前后拦截on_pre_invoke--on_post_invoke)
  - [消息完成回调（on_message_completed）](#消息完成回调on_message_completed)
- [开发 Tool 插件](#开发-tool-插件)
  - [简单工具插件](#简单工具插件)
  - [带沙箱的工具插件](#带沙箱的工具插件)
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

插件独立打包为目录，目录放于 `plugins/` 目录下（或 `nanobee/builtin/` 作为内置插件）。

---

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

对话轮次结束后调用（后台执行，不阻塞 LLM 响应）：

```python
async def on_message_completed(
    self,
    context: PromptBuildContext,
    messages: list[dict[str, Any]],
) -> None:
    """一轮对话结束后调用。

    适用于审计日志、后台整理等非关键同步操作。
    此 Hook 的执行不会阻塞 LLM 响应返回给用户。
    """
    logger.info("对话轮次结束, 消息数: {len(messages)}")
    # 写入审计日志
    async with aiofiles.open("audit.log", "a") as f:
        await f.write(f"{context.context_id}: {len(messages)} 条消息\n")
```

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

### 带沙箱的工具插件

参考 `nanobee/builtin/tool_fs/plugin.py`。沙箱（Sandbox）是框架注入的隔离机制，工具插件可通过 `current_sandbox()` 获取当前请求的沙箱实例：

```python
from nanobee.plugins.tool import ToolPlugin
from nanobee.kernel.sandbox import SandboxViolationError, current_sandbox


class ToolFsPlugin(ToolPlugin):
    name = "tool_fs"
    plugin_type = "tool"

    def _resolve_path(self, path: str) -> str:
        """解析路径并校验沙箱边界。

        使用 current_sandbox() 获取当前请求的沙箱（请求级注入，非全局）。
        """
        sandbox = current_sandbox()
        if sandbox is not None:
            resolved = sandbox.assert_allowed(path)
            return resolved
        # 无沙箱时使用默认工作目录
        return os.path.abspath(path)

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        if tool_name == "read_file":
            safe_path = self._resolve_path(kwargs["path"])
            # ... 读取文件逻辑
            return content
        # ... 其他工具
```

**沙箱规则**：
- 通过 `current_sandbox()` 获取请求级沙箱（ContextVar 注入，线程安全）
- `assert_allowed(path)` 返回规范化绝对路径，或抛出 `SandboxViolationError`
- `SandboxViolationError` 会被框架自动识别为"工作区越界"违规，提示 LLM 修正
- 路径逃逸超过 3 次时会升级为严重警告

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

框架在 `ConversationContext` 中创建 `tmp/` 目录，通过 ContextVar 按请求注入到插件：

```python
class MyPlugin(NanobeePlugin):
    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        # self.tmp 返回 <context_root>/tmp/<plugin_name>/
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
dependencies = ["tool_web"]            # 可选，依赖的其他插件
permissions = ["network"]              # 可选，声明需要的权限

[config]
enabled = true                         # 默认启用
```

**`plugin_type` 字段说明**：

- 可使用 `type` 或 `plugin_type`（优先 `plugin_type`）
- 若有 `plugin_type` 字段，覆盖 `[plugin]` 级别；否则使用 `[plugin].type`

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
4. **沙箱意识**：操作文件、网络时始终使用框架沙箱检测路径
5. **日志代替 print**：使用 `logger = get_logger(__name__)`，配置在 `plugin.py` 中
6. **配置敏感信息**：API Key 等敏感信息通过 `nanobee.yaml` 配置读取，不硬编码
7. **临时文件**：使用 `self.tmp` 目录（框架自动清理），不使用系统临时目录
8. **等幂设计**：`on_load` / `on_enable` 可重复调用而不产生副作用
9. **类型注解**：所有公开方法必须提供完整类型注解
10. **框架无知**：插件不假设框架做了任何智能决策——框架只做机制，策略由 LLM 或插件自主决定
11. **Tool 定义遵循 OpenAI Function Calling 格式**：包含 `name`、`description`、`parameters`，描述须清晰准确以便 LLM 理解
