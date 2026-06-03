"""
Tool Web 插件 - Web 工具
"""


class WebToolPlugin:
    """Web 工具插件"""

    name = "tool-web"
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
        return ["web_search", "web_fetch", "web_scrape"]
