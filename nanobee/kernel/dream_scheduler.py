"""
梦境调度器 - 调度 Agent 的后台梦境任务
"""


class DreamScheduler:
    """梦境调度器"""

    def __init__(self, kernel):
        self.kernel = kernel
        self.dreams = []

    def schedule(self, dream):
        """调度梦境任务"""
        self.dreams.append(dream)

    def run_dreams(self):
        """执行梦境任务"""
        for dream in self.dreams:
            dream.execute()
