# 贡献指南

感谢你对 **nanobee** 的贡献兴趣！无论你打算修复 bug、添加新功能、完善文档还是改进代码质量，你的帮助都将使这个项目变得更好。

## 目录

- [快速开始](#快速开始)
- [开发环境设置](#开发环境设置)
- [代码规范](#代码规范)
- [提交 PR 的流程](#提交-pr-的流程)
- [测试指南](#测试指南)
- [插件开发指南](#插件开发指南)
- [文档贡献](#文档贡献)
- [行为准则](#行为准则)

## 快速开始

### 1. Fork 仓库

点击 GitHub 右上角的 **Fork** 按钮，将仓库 fork 到你的账号下。

### 2. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/nanobee.git
cd nanobee
```

### 3. 安装开发依赖

```bash
# 使用 pip
pip install -e ".[dev]"

# 或使用 uv（推荐）
uv pip install -e ".[dev]"
```

### 4. 创建功能分支

```bash
git checkout -b feature/your-feature-name
# 或
git checkout -b fix/your-bug-fix
```

## 开发环境设置

### 环境要求

- **Python** >= 3.11
- **pip** 或 **uv**（推荐使用 uv）
- **git** >= 2.30

### 推荐工具

- **pre-commit** — 自动代码格式化与检查
- **pytest** — 运行测试
- **mypy** — 类型检查
- **pydocstyle** — docstring 检查

安装开发工具：

```bash
pip install pre-commit mypy pydocstyle pytest-cov
pre-commit install
```

## 代码规范

nanobee 遵循严格的代码规范，确保代码库的一致性和可维护性。

### 格式规范

- **PEP 8**：遵循 Python 官方编码规范
- **缩进**：4 空格（禁止使用 Tab）
- **行宽**：不超过 120 字符
- **引号**：统一使用双引号 `"`
- **文件编码**：UTF-8

### 类型注解

- **公开函数**必须提供类型注解
- **核心模块**使用 `mypy --strict` 强制检查
- **胶水代码**和**老旧模块**可配置豁免

示例：

```python
async def process_message(
    message: str,
    context_id: str,
    timeout: int = 30
) -> dict[str, any]:
    """处理用户消息并返回结果。

    Args:
        message: 用户输入的消息内容
        context_id: 上下文标识符
        timeout: 超时时间（秒），默认为 30

    Returns:
        包含处理结果的字典

    Raises:
        TimeoutError: 处理超时时抛出
        ValueError: 输入参数无效时抛出
    """
    ...
```

### 文档字符串

- 使用 **Google 风格** docstring
- 所有公开接口必须有 docstring
- 使用 `pydocstyle` 检查

示例：

```python
def calculate_context_tokens(messages: list[dict]) -> int:
    """计算消息列表的 token 数量。

    该函数遍历所有消息并统计总 token 数，用于上下文窗口管理。

    Args:
        messages: 消息列表，每条消息包含 role 和 content 字段

    Returns:
        总 token 数量

    Examples:
        >>> messages = [{"role": "user", "content": "你好"}]
        >>> calculate_context_tokens(messages)
        5
    """
    ...
```

### 注释规范

- **核心代码**必须使用中文注释
- 注释应**准确且完整**地解释代码意图
- 避免无意义的注释（如 `i += 1  # 增加 i`）

### 依赖管理

- **生产/开发依赖分离**：在 `pyproject.toml` 中明确区分
- **固定版本**：使用 `>=` 指定最低版本，避免意外破坏
- **禁止动态安装**：代码中禁止使用 `pip install` 或 `importlib` 动态安装

### 资源管理

- **IO、网络、数据库连接**必须使用 `with` 上下文管理
- 大文件必须**流式读取**，禁止一次性全量加载到内存

### 异常处理

- **禁止裸 `except`**：必须捕获具体异常类型
- 使用 `logger.exception()` 记录堆栈信息
- 异常信息应清晰明了

示例：

```python
import logging

logger = logging.getLogger(__name__)

async def fetch_data(url: str) -> str:
    """从远程 URL 获取数据。"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPStatusError as e:
        logger.exception(f"HTTP 请求失败: {e.response.status_code}")
        raise
    except httpx.RequestError as e:
        logger.exception(f"网络请求异常: {e}")
        raise
```

### 封装规范

- 内部方法使用**单下划线** `_` 前缀
- 外部调用内部方法视为违规

### 入口规范

- 所有执行逻辑放在 `if __name__ == "__main__": main()` 中
- 模块应可安全导入而不产生副作用

### 安全规范

- **严格输入校验**：使用 `pydantic` 验证用户输入
- **SQL 参数化**：禁止字符串拼接 SQL
- **禁止 `eval`**：不执行不信任的 `pickle` 数据
- **隐私数据脱敏**：日志中禁止记录敏感信息（API Key、密码等）

## 提交 PR 的流程

### 1. 提交 Commit

```bash
git add .
git commit -m "feat: 添加 XX 功能"
# 或
git commit -m "fix: 修复 XX 问题"
```

**Commit 消息格式**（遵循 [Conventional Commits](https://www.conventionalcommits.org/)）：

- `feat:` 新功能
- `fix:` bug 修复
- `docs:` 文档更新
- `style:` 代码格式（不影响代码运行）
- `refactor:` 重构（既不是新功能也不是修复）
- `test:` 测试相关
- `chore:` 构建过程或辅助工具变动

### 2. 运行检查

```bash
# 运行测试
python -m pytest tests/ -v

# 运行类型检查
mypy --strict nanobee/

# 运行 docstring 检查
pydocstyle nanobee/

# 查看覆盖率
python -m pytest tests/ -v --cov=nanobee --cov-report=term-missing
```

### 3. 推送并提交 PR

```bash
git push origin feature/your-feature-name
```

然后在 GitHub 上提交 **Pull Request**，并填写以下信息：

- **标题**：简明扼要地描述改动
- **描述**：详细说明改动内容、动机和影响
- **关联 Issue**：如果修复了某个 Issue，请引用（如 `Fixes #123`）
- **测试说明**：说明如何验证你的改动

### PR 审查标准

- [ ] 代码符合规范（PEP 8、类型注解、docstring）
- [ ] 测试覆盖率达到要求（核心模块 100%）
- [ ] 所有 CI 检查通过
- [ ] 代码无安全风险
- [ ] 文档已更新（如有功能变化）

## 测试指南

### 运行测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 运行特定测试文件
python -m pytest tests/test_kernel.py -v

# 运行特定测试函数
python -m pytest tests/test_kernel.py::test_kernel_init -v

# 查看覆盖率
python -m pytest tests/ -v --cov=nanobee --cov-report=term-missing
```

### 编写测试

- **核心模块必须 100% 覆盖**
- 测试用例必须有**明确断言**（禁止无断言的测试）
- 使用 `pytest` 框架，遵循命名规范：`test_` 前缀
- 异步测试使用 `pytest-asyncio`

示例：

```python
import pytest
from nanobee.kernel import NanobeeKernel

async def test_kernel_initialization():
    """测试内核初始化。"""
    kernel = NanobeeKernel(config_path="nanobee.yaml")
    assert kernel is not None
    assert kernel.plugin_manager is not None
```

### 测试覆盖要求

| 模块类型 | 覆盖率要求 |
|---------|-----------|
| 核心模块（kernel、agent） | 100% |
| 插件接口（plugins） | 100% |
| CLI 模块（cli） | >= 80% |
| 工具实现（builtin/tool-*） | >= 70% |

## 插件开发指南

### 插件结构

每个插件应包含以下文件：

```
my-plugin/
├── plugin.py      # 插件实现
├── plugin.toml    # 插件元数据
└── README.md      # 插件说明（可选）
```

### 插件元数据（plugin.toml）

```toml
[plugin]
name = "tool-my-tool"
version = "1.0.0"
description = "我的工具插件描述"
author = "Your Name"
license = "MIT"
```

### 创建插件

使用 CLI 命令创建插件骨架：

```bash
nanobee plugin create my-plugin
```

### 插件类型

| 插件类型 | 接口类 | 用途 |
|---------|--------|------|
| ChannelPlugin | `nanobee.plugins.channel.ChannelPlugin` | 通信渠道（CLI、HTTP、Telegram 等） |
| ToolPlugin | `nanobee.plugins.tool.ToolPlugin` | 工具调用（文件、Shell、Web 等） |
| MemoryPlugin | `nanobee.plugins.memory.MemoryPlugin` | 记忆存储（文件、数据库、Redis 等） |
| SkillPlugin | `nanobee.plugins.skill.SkillPlugin` | 技能定义 |
| KnowledgePlugin | `nanobee.plugins.knowledge.KnowledgePlugin` | 知识库查询 |
| DreamPlugin | `nanobee.plugins.dream.DreamPlugin` | 后台梦境任务 |

详见 [README.md#插件开发](README.md#插件开发) 中的完整示例。

## 文档贡献

### 文档类型

- **README.md**：项目概述、快速开始、架构说明
- **CONTRIBUTING.md**：本文件，贡献指南
- **LICENSE**：许可证
- **docs/**：详细文档（待完善）

### 文档规范

- 使用 **Markdown** 格式
- 标题层级：`#` -> `##` -> `###`
- 代码块标注语言（`python`、`bash`、`toml`）
- 链接使用相对路径或完整 URL

### 更新文档

当你添加新功能或修改行为时，请同步更新相关文档：

- 修改 CLI 命令 -> 更新 README.md
- 修改插件接口 -> 更新插件开发指南
- 新增配置选项 -> 更新配置文件说明

## 行为准则

### 我们的承诺

为了营造一个开放和友好的环境，我们承诺：

- **尊重**：尊重不同的观点和经验
- **包容**：无论背景、经验或性别认同，欢迎所有人
- **建设性**：提供建设性的反馈，避免人身攻击
- **专业**：保持专业态度，理性讨论

### 不可接受的行为

- 使用性暗示的语言或图像
- 人身攻击或侮辱性评论
- 公开或私下骚扰
- 未经许可发布他人隐私信息
- 其他不道德或不专业的行为

### 如何报告

如果你遇到不可接受的行为，请通过项目维护者的联系方式报告。

所有贡献者应遵守项目的 [行为准则](CODE_OF_CONDUCT.md)（待创建）。

## 问题反馈

### 报告 Bug

在报告 Bug 之前，请搜索 [现有 Issue](https://github.com/YOUR_REPO/issues) 避免重复。

报告 Bug 时请提供：

- **清晰的问题描述**
- **复现步骤**
- **预期行为 vs 实际行为**
- **环境信息**（Python 版本、操作系统、nanobee 版本）
- **日志输出**（如有）

### 功能建议

我们欢迎新功能建议！提出建议时请说明：

- 功能解决的问题
- 使用场景示例
- 可能的实现思路（可选）

## 致谢

感谢所有为 nanobee 做出贡献的人！

特别感谢：
- [nanobot](https://github.com/HKUDS/nanobot) 项目，nanobee 基于其衍生开发
- 所有贡献者和用户

---

**再次感谢你的贡献！** 🐝
