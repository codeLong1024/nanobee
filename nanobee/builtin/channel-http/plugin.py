"""
Channel HTTP 插件 - HTTP 通信渠道
"""


class HTTPChannelPlugin:
    """HTTP 渠道插件"""

    name = "channel-http"
    version = "0.0.1"

    def __init__(self):
        self.kernel = None
        self.server = None

    def initialize(self, kernel):
        """初始化"""
        self.kernel = kernel

    def connect(self, host="0.0.0.0", port=8080):
        """连接"""
        pass

    def disconnect(self):
        """断开连接"""
        pass

    def send(self, message):
        """发送消息"""
        pass

    def receive(self):
        """接收消息"""
        pass
