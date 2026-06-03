"""
DreamPlugin 接口 - 梦境插件接口
"""

from .base import NanobeePlugin


class DreamPlugin(NanobeePlugin):
    """梦境插件基类"""

    name = "dream"

    def execute(self):
        """执行梦境任务"""
        pass

    def get_description(self):
        """获取梦境描述"""
        pass
