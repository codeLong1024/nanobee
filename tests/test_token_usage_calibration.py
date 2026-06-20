"""测试从 LLM API 响应中提取真实 token 消耗并校准估算。

验证 OpenAICompatProvider._extract_usage() 能从 DeepSeek/自定义端点
的响应中提取 prompt_tokens、completion_tokens、cached_tokens 等信息，
并用于校准本地字符比例估算。
"""

import pytest


class TestTokenUsageExtraction:
    """测试 token 用量提取功能。"""

    def test_extract_usage_from_dict_response(self):
        """从 dict 格式的响应中提取 token 用量。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        response_dict = {
            "usage": {
                "prompt_tokens": 150,
                "completion_tokens": 80,
                "total_tokens": 230,
            }
        }

        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage["prompt_tokens"] == 150
        assert usage["completion_tokens"] == 80
        assert usage["total_tokens"] == 230

    def test_extract_usage_from_sdk_response(self):
        """从 SDK 对象格式的响应中提取 token 用量。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        # 模拟 SDK 对象
        usage_obj = type("Usage", (), {
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
        })()
        response_obj = type("Response", (), {"usage": usage_obj})()

        usage = OpenAICompatProvider._extract_usage(response_obj)

        assert usage["prompt_tokens"] == 200
        assert usage["completion_tokens"] == 100
        assert usage["total_tokens"] == 300

    def test_extract_usage_with_cached_tokens(self):
        """提取包含缓存命中 token 的用量。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        # DeepSeek 格式：prompt_cache_hit_tokens
        response_dict = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "prompt_cache_hit_tokens": 300,
            }
        }

        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage["prompt_tokens"] == 500
        assert usage["completion_tokens"] == 200
        assert usage["cached_tokens"] == 300

    def test_extract_usage_with_prompt_tokens_details_cached(self):
        """提取包含 prompt_tokens_details.cached_tokens 的用量。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        # OpenAI/Zhipu 格式：prompt_tokens_details.cached_tokens
        response_dict = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "prompt_tokens_details": {
                    "cached_tokens": 300,
                },
            }
        }

        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage["cached_tokens"] == 300

    def test_extract_usage_empty_response(self):
        """空响应返回空字典。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        response_dict = {}
        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage == {}

    def test_extract_usage_missing_usage_field(self):
        """响应中没有 usage 字段返回空字典。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        response_dict = {
            "choices": [
                {
                    "message": {"content": "Hello", "role": "assistant"},
                    "finish_reason": "stop",
                }
            ]
        }

        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage == {}

    def test_extract_usage_zero_values(self):
        """零值 token 正常返回。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        response_dict = {
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        }

        usage = OpenAICompatProvider._extract_usage(response_dict)

        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0
        assert usage["total_tokens"] == 0


class TestTokenCalibration:
    """测试 token 估算校准功能。"""

    def test_char_ratio_calibration(self):
        """用真实 token 值校准字符比例。"""
        # 模拟场景：发送 30 个中文字符，API 返回 18 tokens
        # 字符/token 比例 = 30 / 18 = 1.67
        # 校准后系数 = 1.67（意味着 1 token ≈ 1.67 字符）

        history = []
        sent_text = "你是一个专业的客服助手"  # 11 个中文字符
        actual_tokens = 7  # 假设 API 返回

        # 计算比例
        ratio = len(sent_text) / actual_tokens  # 11 / 7 ≈ 1.57

        # 用滑动窗口平滑
        history.append(ratio)
        calibrated_ratio = sum(history) / len(history)

        # 验证校准系数合理
        assert 1.0 < calibrated_ratio < 3.0

    def test_estimate_vs_actual_comparison(self):
        """对比估算值与实际值的差异。"""
        from nanobee.utils.helpers import estimate_prompt_tokens

        messages = [
            {"role": "system", "content": "你是一个专业的客服助手，请回答用户问题。"},
            {"role": "user", "content": "你好，我想查询公司信息"},
        ]

        estimated = estimate_prompt_tokens(messages)

        # 估算值应该 > 0
        assert estimated > 0

        # 估算值应该在合理范围内（10-500 tokens）
        assert 10 <= estimated <= 500


class TestTokenUsageInLLMResponse:
    """测试 LLMResponse 中的 usage 字段。"""

    def test_llm_response_with_usage(self):
        """LLMResponse 包含 token 用量信息。"""
        from nanobee.providers.base import LLMResponse

        response = LLMResponse(
            content="你好！",
            finish_reason="stop",
            usage={
                "prompt_tokens": 150,
                "completion_tokens": 20,
                "total_tokens": 170,
                "cached_tokens": 100,
            },
        )

        assert response.content == "你好！"
        assert response.usage["prompt_tokens"] == 150
        assert response.usage["completion_tokens"] == 20
        assert response.usage["total_tokens"] == 170
        assert response.usage["cached_tokens"] == 100

    def test_llm_response_without_usage(self):
        """LLMResponse 没有 token 用量信息时默认为空字典。"""
        from nanobee.providers.base import LLMResponse

        response = LLMResponse(
            content="你好！",
            finish_reason="stop",
        )

        assert response.usage == {}


class TestCacheHitDetection:
    """测试缓存命中检测。"""

    def test_deepseek_cache_format(self):
        """DeepSeek 格式的缓存 token 提取。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        # DeepSeek 使用 prompt_cache_hit_tokens
        response = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "prompt_cache_hit_tokens": 300,
            }
        }

        usage = OpenAICompatProvider._extract_usage(response)

        assert usage["cached_tokens"] == 300
        assert usage["prompt_tokens"] == 500

    def test_openai_cache_format(self):
        """OpenAI 格式的缓存 token 提取。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        # OpenAI 使用 prompt_tokens_details.cached_tokens
        response = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "prompt_tokens_details": {
                    "cached_tokens": 300,
                },
            }
        }

        usage = OpenAICompatProvider._extract_usage(response)

        assert usage["cached_tokens"] == 300

    def test_cache_miss_calculation(self):
        """计算缓存未命中 token。"""
        from nanobee.providers.openai_compat_provider import OpenAICompatProvider

        response = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
                "prompt_cache_hit_tokens": 300,
            }
        }

        usage = OpenAICompatProvider._extract_usage(response)

        # prompt_tokens - cache_hit = cache_miss
        cache_miss = usage["prompt_tokens"] - usage["cached_tokens"]
        assert cache_miss == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
