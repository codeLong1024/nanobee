"""
PluginHookMixin - 插件 Hook 混入类

定义 5 个核心 Hook 接口,插件可以通过混入此类并覆盖方法,
在 Agent 生命周期的关键切面注入逻辑。

所有方法都有默认空实现,插件只需覆盖需要的。
"""

from __future__ import annotations

from typing import Any


class PluginHookMixin:
    """插件 Hook 混入类。

    提供 5 个生命周期 Hook 的默认实现,消除代码重复。
    ``NanobeePlugin`` 默认继承此类,所有插件自动拥有这些 Hook 方法。

    使用示例::

        class MyMemoryPlugin(NanobeePlugin):
            def contribute_to_prompt(self, context):
                return f\"# Memory\\n{context.user_id} 的记忆内容\"

            async def on_message_completed(self, context, messages):
                await self.store_latest_memory(messages)

    注意:即使不显式继承此类,所有 ``NanobeePlugin`` 子类也自动拥有
    这 5 个 Hook 方法(通过继承链获得)。
    """

    def contribute_to_prompt(self, context: Any) -> str | None:
        """向 System Prompt 注入文本。

        Args:
            context: 当前用户上下文(UserContext 实例)

        Returns:
            注入的文本段,返回 None 或空字符串表示不注入
        """
        return None

    def contribute_to_tools(
        self,
        context: Any,
        current_tool_names: list[str],
    ) -> list[str]:
        """动态增删工具列表。

        Args:
            context: 当前用户上下文(UserContext 实例)
            current_tool_names: 当前已注册的工具名称列表

        Returns:
            修改后的工具名称列表
        """
        return current_tool_names

    async def on_pre_invoke(
        self,
        context: Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """工具执行前拦截。

        可用于鉴权、修改参数、记录日志等。
        抛出异常将阻止工具执行,异常消息会返回给 LLM。

        Args:
            context: 当前用户上下文(UserContext 实例)
            tool_name: 工具名称
            args: 工具参数字典

        Returns:
            可修改后的参数字典
        """
        return args

    async def on_post_invoke(
        self,
        context: Any,
        tool_name: str,
        result: Any,
    ) -> Any:
        """工具执行后拦截。

        可用于修改结果、触发副作用(如写入记忆)、统计调用次数等。

        Args:
            context: 当前用户上下文(UserContext 实例)
            tool_name: 工具名称
            result: 工具返回结果

        Returns:
            可修改后的结果
        """
        return result

    async def on_message_completed(
        self,
        context: Any,
        messages: list[dict[str, Any]],
    ) -> None:
        """对话轮次结束后的生命周期 Hook。

        在每轮 Agent 交互完成后异步调用，适用于后台整理（如写入长期记忆）、
        审计日志、梦境调度等场景。框架不阻塞 LLM 响应，调度策略由插件通过
        ``plugin.toml`` 的 ``[hooks.on_message_completed]`` 段声明：

        - ``block_next = true`` → 框架在下一轮同 context 的 dispatch 前等待本 Hook 完成
        - ``priority = 100``  → 参与组内排序，数值越大越优先执行

        未声明时默认 ``block_next=false, priority=10``，非阻塞、无顺序保证。

        Args:
            context: 当前用户上下文(UserContext 实例)
            messages: 本轮完整的消息列表
        """
        pass
