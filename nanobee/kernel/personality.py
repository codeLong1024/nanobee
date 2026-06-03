"""
人格指纹 - 定义 Agent 的人格特征
"""


class Personality:
    """人格指纹"""

    def __init__(self, name, traits=None):
        self.name = name
        self.traits = traits or {}

    def get_trait(self, key, default=None):
        """获取人格特质"""
        return self.traits.get(key, default)

    def set_trait(self, key, value):
        """设置人格特质"""
        self.traits[key] = value
