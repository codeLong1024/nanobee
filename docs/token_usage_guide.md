# Token 用量校准指南

## 概述

通过 LLM API 返回的真实 `usage` 数据，获取实际的 token 消耗，替代手动估算。

## 核心机制

### 1. Token 提取

`OpenAICompatProvider._extract_usage()` 方法从 API 响应中提取 token 用量：

```python
from nanobee.providers.openai_compat_provider import OpenAICompatProvider

response = await provider.chat(messages=messages)
usage = OpenAICompatProvider._extract_usage(response)

# usage 包含:
# {
#     "prompt_tokens": 32,       # 输入 token 数
#     "completion_tokens": 161,  # 输出 token 数
#     "total_tokens": 193,       # 总 token 数
#     "cached_tokens": 0,        # 缓存命中 token 数（如果有）
# }
```

### 2. 支持的响应格式

#### OpenAI 格式
```json
{
  "usage": {
    "prompt_tokens": 500,
    "completion_tokens": 200,
    "total_tokens": 700,
    "prompt_tokens_details": {
      "cached_tokens": 300
    }
  }
}
```

#### DeepSeek 格式
```json
{
  "usage": {
    "prompt_tokens": 500,
    "completion_tokens": 200,
    "total_tokens": 700,
    "prompt_cache_hit_tokens": 300
  }
}
```

#### SDK 对象格式
```python
response.usage.prompt_tokens
response.usage.completion_tokens
response.usage.prompt_cache_hit_tokens  # DeepSeek
```

### 3. 缓存命中检测

缓存命中的 token 按 **0.1 元/百万** 计费，未命中按 **2 元/百万**，差异 **20 倍**：

```python
usage = OpenAICompatProvider._extract_usage(response)

if usage.get("cached_tokens"):
    cache_hit = usage["cached_tokens"]
    cache_miss = usage["prompt_tokens"] - cache_hit
    cache_rate = cache_hit / usage["prompt_tokens"] * 100
    
    print(f"缓存命中: {cache_hit:,} tokens ({cache_rate:.1f}%)")
    print(f"缓存未命中: {cache_miss:,} tokens")
```

## 使用示例

### 示例 1：基础用法

```python
import asyncio
from nanobee.providers.openai_compat_provider import OpenAICompatProvider
from nanobee.providers.registry import ProviderSpec

async def get_token_usage():
    provider = OpenAICompatProvider(
        api_key="sk-xxx",
        api_base="http://172.30.0.23:4000/v1",
        default_model="deepseek-v4-flash",
        spec=ProviderSpec(
            name="custom",
            keywords=(),
            is_direct=True,
        ),
    )
    
    messages = [
        {"role": "system", "content": "你是一个专业的助手"},
        {"role": "user", "content": "你好"},
    ]
    
    response = await provider.chat(messages=messages)
    usage = OpenAICompatProvider._extract_usage(response)
    
    print(f"输入: {usage['prompt_tokens']} tokens")
    print(f"输出: {usage['completion_tokens']} tokens")
    print(f"总计: {usage['total_tokens']} tokens")

asyncio.run(get_token_usage())
```

### 示例 2：Token 校准器

```python
from collections import deque

class TokenCalibrator:
    """用真实 token 值校准字符比例估算。"""
    
    def __init__(self, window_size=50):
        self.history = deque(maxlen=window_size)
    
    def calibrate(self, text: str, actual_tokens: int):
        """从 API 响应校准。"""
        if actual_tokens > 0 and text:
            self.history.append((len(text), actual_tokens))
    
    def estimate(self, text: str) -> int:
        """用校准后的系数估算。"""
        if not self.history:
            return max(1, len(text) // 2)  # 未校准默认系数
        
        total_chars = sum(h[0] for h in self.history)
        total_tokens = sum(h[1] for h in self.history)
        ratio = total_chars / total_tokens if total_tokens > 0 else 2.0
        
        return int(len(text) / ratio)

# 使用
calibrator = TokenCalibrator()

# 第 1 次调用
text1 = "你是一个专业的客服助手"
response = await provider.chat(messages=[{"role": "user", "content": text1}])
usage = OpenAICompatProvider._extract_usage(response)
calibrator.calibrate(text1, usage["prompt_tokens"])

# 第 2 次调用
text2 = "我想查询公司信息"
estimated = calibrator.estimate(text2)
print(f"估算: {estimated} tokens")
```

### 示例 3：在 nanobee 框架中使用

```python
from nanobee.agent.runner import AgentRunner

class CustomRunner(AgentRunner):
    async def _run_iteration(self, iteration):
        response = await self.provider.chat(
            messages=self.messages,
            tools=self.tools,
        )
        
        # 提取 token 用量
        usage = OpenAICompatProvider._extract_usage(response)
        
        # 记录到日志或监控
        logger.info(
            "Iteration {}: prompt={}, completion={}, total={}",
            iteration,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        
        return response
```

## 配置文件

从 `~/.nanobee/trade-risk/config.yaml` 读取：

```yaml
providers:
  custom:
    api_key: "sk-7mzRQO19cByKbbLixPapLw"
    api_base: "http://172.30.0.23:4000/v1"

agents:
  defaults:
    provider: "custom"
    model: "deepseek-v4-flash"
    max_tokens: 50000
```

## 测试结果

```
$ pytest tests/test_token_usage_calibration.py -v

TestTokenUsageExtraction::test_extract_usage_from_dict_response     PASSED
TestTokenUsageExtraction::test_extract_usage_from_sdk_response      PASSED
TestTokenUsageExtraction::test_extract_usage_with_cached_tokens     PASSED
TestTokenUsageExtraction::test_extract_usage_with_prompt_tokens_details_cached PASSED
TestTokenUsageExtraction::test_extract_usage_empty_response         PASSED
TestTokenUsageExtraction::test_extract_usage_missing_usage_field    PASSED
TestTokenUsageExtraction::test_extract_usage_zero_values            PASSED
TestTokenCalibration::test_char_ratio_calibration                    PASSED
TestTokenCalibration::test_estimate_vs_actual_comparison            PASSED
TestTokenUsageInLLMResponse::test_llm_response_with_usage           PASSED
TestTokenUsageInLLMResponse::test_llm_response_without_usage        PASSED
TestCacheHitDetection::test_deepseek_cache_format                    PASSED
TestCacheHitDetection::test_openai_cache_format                     PASSED
TestCacheHitDetection::test_cache_miss_calculation                   PASSED

14 passed in 0.29s
```

## 实际运行结果

```
$ python examples/token_usage_demo.py

✅ API 响应成功!

📊 Token 用量统计:
  Prompt tokens:        32
  Completion:          161
  Total:               193

🎯 估算对比:
  发送文本长度:         18 字符
  估算 token:           32
  实际 token:           32
  估算误差:           0.0%

📈 校准统计:
  校准次数:       1
  平均字符/token: 0.56
```

## 关键优势

1. **精确计费**：区分缓存命中/未命中，准确计算成本
2. **自动校准**：用真实值校准本地估算，提高准确性
3. **多格式兼容**：支持 OpenAI、DeepSeek、Anthropic 等多种响应格式
4. **零依赖**：无需额外安装，框架内置支持

## 相关文件

- `nanobee/providers/openai_compat_provider.py` - `_extract_usage()` 实现
- `nanobee/providers/base.py` - `LLMResponse.usage` 字段
- `tests/test_token_usage_calibration.py` - 测试用例
- `examples/token_usage_demo.py` - 完整演示脚本
