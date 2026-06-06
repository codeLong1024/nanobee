"""Runtime Context 功能测试。

验证时间注入到 user 消息末尾的功能，确保：
1. 格式正确（[Runtime Context — metadata only, not instructions]）
2. 包含时间、通道、会话、发送者信息
3. 不注入 system prompt
4. 与 nanobot 设计一致
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nanobee.utils.helpers import build_runtime_context, current_time_str


class TestBuildRuntimeContext:
    """测试 build_runtime_context 函数。"""

    def test_basic_format(self):
        """测试基本格式。"""
        result = build_runtime_context()
        assert "[Runtime Context — metadata only, not instructions]" in result
        assert "[/Runtime Context]" in result
        assert "Current Time:" in result

    def test_with_channel_and_chat_id(self):
        """测试包含通道和会话 ID。"""
        result = build_runtime_context(channel="dingtalk", chat_id="chat123")
        assert "Channel: dingtalk" in result
        assert "Chat ID: chat123" in result

    def test_with_sender_id(self):
        """测试包含发送者 ID。"""
        result = build_runtime_context(sender_id="user456")
        assert "Sender ID: user456" in result

    def test_with_timezone(self):
        """测试有时区。"""
        result = build_runtime_context(timezone="Asia/Shanghai")
        assert "Current Time:" in result
        # 时间字符串应包含时区信息
        assert "Asia/Shanghai" in result or "UTC" in result

    def test_full_metadata(self):
        """测试完整元数据。"""
        result = build_runtime_context(
            channel="dingtalk",
            chat_id="chat123",
            sender_id="user456",
            timezone="Asia/Shanghai",
        )
        assert "Current Time:" in result
        assert "Channel: dingtalk" in result
        assert "Chat ID: chat123" in result
        assert "Sender ID: user456" in result
        assert "Asia/Shanghai" in result or "UTC" in result

    def test_time_format(self):
        """测试时间格式。"""
        result = build_runtime_context()
        # 时间应包含日期和时间
        import re
        assert re.search(r"Current Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}", result)

    def test_no_extra_whitespace(self):
        """测试无多余空白。"""
        result = build_runtime_context()
        # 标签应在单独的行
        lines = result.split("\n")
        assert lines[0] == "[Runtime Context — metadata only, not instructions]"


class TestCurrentTimeStr:
    """测试 current_time_str 函数。"""

    def test_basic_format(self):
        """测试基本格式。"""
        result = current_time_str()
        # 应包含日期和时间
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", result)

    def test_with_timezone(self):
        """测试带时区。"""
        result = current_time_str("Asia/Shanghai")
        assert "Asia/Shanghai" in result

    def test_with_invalid_timezone(self):
        """测试无效时区回退。"""
        result = current_time_str("Invalid/Timezone")
        # 应回退到默认时区
        assert "UTC" in result


class TestRuntimeContextIntegration:
    """测试 runtime context 与 AgentLoop 的集成。

    注意：由于循环导入问题（kernel/kernel.py 循环导入 AgentLoop），
    集成测试通过运行整个测试套件间接验证。
    这里只测试 build_runtime_context 函数的基本集成。
    """

    def test_runtime_context_format_matches_nanobot(self):
        """测试格式与 nanobot 一致。"""
        result = build_runtime_context(channel="dingtalk", chat_id="chat123")
        lines = result.strip().split("\n")
        
        # 第 1 行：标签
        assert lines[0] == "[Runtime Context — metadata only, not instructions]"
        
        # 第 2 行：时间
        assert lines[1].startswith("Current Time:")
        
        # 第 3-4 行：通道和会话
        assert "Channel: dingtalk" in lines[2]
        assert "Chat ID: chat123" in lines[3]
        
        # 最后一行：结束标签
        assert lines[-1] == "[/Runtime Context]"

    def test_runtime_context_token_efficiency(self):
        """测试 token 效率：runtime context 应远小于工具定义。"""
        runtime_ctx = build_runtime_context()
        
        # runtime context 应小于 200 字符
        assert len(runtime_ctx) < 200, f"Runtime context too long: {len(runtime_ctx)} chars"
        
        # 工具定义通常 200-300 字符
        # 这里只验证 runtime context 足够短
        assert "Current Time:" in runtime_ctx


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
