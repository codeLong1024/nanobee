"""
Tool MCP 插件 - MCP 工具
"""


class MCPToolPlugin:
    """MCP 工具插件"""

    name = "tool-mcp"
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
        return ["mcp_call", "mcp_list"]
