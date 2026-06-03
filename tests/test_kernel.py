"""内核集成测试"""

from __future__ import annotations

import pytest

from nanobee.kernel import NanobeeKernel
from nanobee.kernel.core_parser import CoreMDParser


@pytest.fixture
def temp_core_md(tmp_path):
    """创建临时 core.md 文件"""
    content = """# test core.md

## Soul
你是测试助手。

## Rules
- 保持简洁
"""
    core_md = tmp_path / "core.md"
    core_md.write_text(content, encoding="utf-8")
    return core_md


def test_core_md_parser(temp_core_md):
    """测试 core.md 解析器"""
    parser = CoreMDParser(temp_core_md)
    sections = parser.parse()

    assert "Soul" in sections
    assert "Rules" in sections
    assert "你是测试助手" in sections["Soul"]
    assert "保持简洁" in sections["Rules"]


def test_core_md_parser_hash(temp_core_md):
    """测试哈希计算"""
    parser = CoreMDParser(temp_core_md)
    hash1 = parser.compute_hash()
    hash2 = parser.compute_hash()

    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 = 64 hex chars


@pytest.mark.asyncio
async def test_kernel_boot(tmp_path):
    """测试内核启动"""
    config = {
        "work_dir": str(tmp_path),
        "core_md_path": str(tmp_path / "core.md"),
    }

    # 创建 core.md
    CoreMDParser.create_default(tmp_path / "core.md")

    kernel = NanobeeKernel(config=config)
    await kernel.boot()

    assert kernel.is_booted

    await kernel.shutdown()
    assert not kernel.is_booted
