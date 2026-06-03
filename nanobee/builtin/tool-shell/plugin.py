"""
Tool Shell 插件 - Shell 命令工具
"""


class ShellToolPlugin:
    """Shell 工具插件"""

    name = "tool-shell"
    version = "0.0.1"

    def __init__(self):
        self.kernel = None

    def initialize(self, kernel):
        """初始化"""
        self.kernel = kernel

    def call(self, tool_name, *args, **kwargs):
        """调用工具"""
        pass

    def list_tools(self):
        """列出可用工具"""
        return ["shell_execute", "shell_script"]
