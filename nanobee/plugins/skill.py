"""
SkillPlugin 接口 - 技能插件接口
"""

from .base import NanobeePlugin


class SkillPlugin(NanobeePlugin):
    """技能插件基类"""

    name = "skill"

    def execute(self, *args, **kwargs):
        """执行技能"""
        pass

    def get_skills(self):
        """获取可用技能列表"""
        pass
