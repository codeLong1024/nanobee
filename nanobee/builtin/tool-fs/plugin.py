"""
Tool FS 插件 - 文件系统工具
"""


class FileSystemToolPlugin:
    """文件系统工具插件"""

    name = "tool-fs"
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
        return ["read_file", "write_file", "list_dir", "delete_file"]
