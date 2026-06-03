"""
KnowledgePlugin 接口 - 知识库插件接口
"""

from .base import NanobeePlugin


class KnowledgePlugin(NanobeePlugin):
    """知识库插件基类"""

    name = "knowledge"

    def query(self, question):
        """查询知识"""
        pass

    def add(self, knowledge):
        """添加知识"""
        pass

    def update(self, knowledge_id, knowledge):
        """更新知识"""
        pass

    def delete(self, knowledge_id):
        """删除知识"""
        pass
