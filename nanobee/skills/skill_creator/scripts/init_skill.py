#!/usr/bin/env python3
"""
技能初始化器 —— 从模板创建新的技能目录。

用法:
    init_skill.py <skill-name> --path <path> [--resources scripts,references,assets] [--examples]

示例:
    init_skill.py pdf_editor --path ./my_skills
    init_skill.py pdf_editor --path ./my_skills --resources scripts,references
    init_skill.py weekly_report --path ./my_skills --resources scripts --examples

注意: nanobee 技能使用 snake_case 命名（如 pdf_editor、git_log_analyzer）。
"""

import argparse
import re
import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCES = {"scripts", "references", "assets"}

SKILL_TEMPLATE = """---
name: {skill_name}
description: "[TODO: 完整说明技能的功能和何时使用。包括触发场景 —— 哪些类型的文件、任务或用户请求应该触发这个技能。]"
# 如果是每轮对话都必须注入的技能（如记忆管理），取消下面注释：
# full_inject: true
---

# {skill_title}

## 概述

[TODO: 1-2 句话说明这个技能的作用]

## 结构选择

[TODO: 根据技能用途选择合适的结构，完成后删除此节]

**1. 工作流式**（适合顺序流程）
- 示例：DOCX 技能 → 工作流决策树 → 读取 → 创建 → 编辑
- 结构：## 概述 -> ## 工作流决策树 -> ## 步骤 1 -> ## 步骤 2...

**2. 任务式**（适合工具集合）
- 示例：PDF 技能 → 快速开始 -> 合并 PDF -> 拆分 PDF -> 提取文本
- 结构：## 概述 -> ## 快速开始 -> ## 任务类别 1 -> ## 任务类别 2...

**3. 参考/指南式**（适合标准或规范）
- 示例：品牌风格 → 品牌规范 -> 色彩 -> 字体 -> 视觉元素
- 结构：## 概述 -> ## 规范 -> ## 说明 -> ## 使用...

**4. 能力式**（适合集成系统）
- 示例：产品管理 → 核心能力 -> 1. 功能 -> 2. 功能...
- 结构：## 概述 -> ## 核心能力 -> ### 1. 功能...

## [TODO: 替换为第一个主要章节]

[TODO: 添加具体内容：
- 技术技能的代码示例
- 复杂工作流的决策树
- 现实用户请求的示例
- 引用脚本/模板/参考文件]

## 资源（可选）

仅创建技能实际需要的资源目录。如果无需任何资源，删除此节。

### scripts/
可执行代码（Python/Bash 等），可直接运行执行特定操作。

**适合放入**：Python 脚本、Shell 脚本、或执行自动化、数据处理等操作的任何可执行代码。

**注意**：脚本可直接运行而无需读入上下文，但仍可被 Agent 读取以进行补丁或环境调整。

### references/
按需加载到上下文的文档和参考资料。

**适合放入**：深入文档、API 参考、数据库模式、完整指南、或 Agent 在工作时需参考的详细信息。

### assets/
不需要读入上下文、但在 Agent 输出中使用的文件。

**适合放入**：模板、样板代码、文档模板、图片、图标、字体、或任何在最终输出中被复制或使用的文件。

---

**并非每个技能都需要三种类型的资源。**
"""

EXAMPLE_SCRIPT = '''#!/usr/bin/env python3
"""
{skill_name} 的示例辅助脚本

这是一个占位脚本，可以直接执行。
如果是实际技能，请替换为具体实现或删除此文件。

实际技能中的脚本示例：
- pdf_editor/scripts/rotate_pdf.py - 旋转 PDF 页面
- pdf_editor/scripts/extract_images.py - 提取 PDF 中的图片
"""

def main():
    print("这是 {skill_name} 的示例脚本")
    # TODO: 添加实际的脚本逻辑

if __name__ == "__main__":
    main()
'''

EXAMPLE_REFERENCE = """# {skill_title} 参考文档

这是一个占位参考文档。
如果是实际技能，请替换为具体内容或删除此文件。

## 参考文档的用途

参考文档适用于：
- 完整的 API 文档
- 详细的工作流指南
- 复杂的多步骤流程
- 对 SKILL.md 来说过于冗长的信息
- 只在特定场景下才需要的内容

## 结构建议

### API 参考示例
- 概述
- 鉴权
- 端点及示例
- 错误码
- 频率限制

### 工作流指南示例
- 前置条件
- 分步说明
- 常见模式
- 故障排除
- 最佳实践
"""

EXAMPLE_ASSET = """# 示例资源文件

这个占位文件代表资源文件的存放位置。
如果是实际技能，请替换为实际资源文件或删除此文件。

资源文件不需要读入上下文，而是在 Agent 输出中使用。

常见资源类型：
- 模板：.pptx、.docx、项目样板目录
- 图片：.png、.jpg、.svg、.gif
- 字体：.ttf、.otf、.woff、.woff2
- 图标：.ico、.svg
- 数据文件：.csv、.json、.xml、.yaml
"""


def normalize_skill_name(raw_name: str) -> str:
    """规范化技能名为 snake_case。"""
    name = raw_name.strip().lower()
    # 替换连字符/空格为下划线，移除非法字符
    name = re.sub(r"[^a-z0-9_]+", "_", name)
    # 去重下划线
    name = re.sub(r"_+", "_", name)
    # 去除首尾下划线
    name = name.strip("_")
    return name


def title_case_skill_name(skill_name: str) -> str:
    """将 snake_case 技能名转换为 Title Case 用于显示。"""
    return " ".join(word.capitalize() for word in skill_name.split("_"))


def parse_resources(raw_resources: str) -> list[str]:
    """解析 --resources 参数。"""
    if not raw_resources:
        return []
    resources = [item.strip() for item in raw_resources.split(",") if item.strip()]
    invalid = sorted({item for item in resources if item not in ALLOWED_RESOURCES})
    if invalid:
        allowed = ", ".join(sorted(ALLOWED_RESOURCES))
        print(f"[ERROR] 未知的资源类型: {', '.join(invalid)}")
        print(f"   允许的类型: {allowed}")
        sys.exit(1)
    deduped = []
    seen = set()
    for resource in resources:
        if resource not in seen:
            deduped.append(resource)
            seen.add(resource)
    return deduped


def create_resource_dirs(
    skill_dir: Path, skill_name: str, skill_title: str,
    resources: list[str], include_examples: bool,
) -> None:
    """创建资源目录和可选的示例文件。"""
    for resource in resources:
        resource_dir = skill_dir / resource
        resource_dir.mkdir(exist_ok=True)
        if resource == "scripts":
            if include_examples:
                example_script = resource_dir / "example.py"
                example_script.write_text(EXAMPLE_SCRIPT.format(skill_name=skill_name))
                example_script.chmod(0o755)
                print("[OK] 创建 scripts/example.py")
            else:
                print("[OK] 创建 scripts/")
        elif resource == "references":
            if include_examples:
                example_reference = resource_dir / "api_reference.md"
                example_reference.write_text(EXAMPLE_REFERENCE.format(skill_title=skill_title))
                print("[OK] 创建 references/api_reference.md")
            else:
                print("[OK] 创建 references/")
        elif resource == "assets":
            if include_examples:
                example_asset = resource_dir / "example_asset.txt"
                example_asset.write_text(EXAMPLE_ASSET)
                print("[OK] 创建 assets/example_asset.txt")
            else:
                print("[OK] 创建 assets/")


def init_skill(
    skill_name: str, path: str, resources: list[str], include_examples: bool,
) -> Path | None:
    """初始化新技能。"""
    # 确定技能目录路径
    skill_dir = Path(path).resolve() / skill_name

    # 检查是否已存在
    if skill_dir.exists():
        print(f"[ERROR] 技能目录已存在: {skill_dir}")
        return None

    # 创建技能目录
    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
        print(f"[OK] 创建技能目录: {skill_dir}")
    except Exception as e:
        print(f"[ERROR] 创建目录失败: {e}")
        return None

    # 创建 SKILL.md
    skill_title = title_case_skill_name(skill_name)
    skill_content = SKILL_TEMPLATE.format(skill_name=skill_name, skill_title=skill_title)

    skill_md_path = skill_dir / "SKILL.md"
    try:
        skill_md_path.write_text(skill_content)
        print("[OK] 创建 SKILL.md")
    except Exception as e:
        print(f"[ERROR] 创建 SKILL.md 失败: {e}")
        return None

    # 创建资源目录
    if resources:
        try:
            create_resource_dirs(skill_dir, skill_name, skill_title, resources, include_examples)
        except Exception as e:
            print(f"[ERROR] 创建资源目录失败: {e}")
            return None

    # 输出下一步
    print(f"\n[OK] 技能 '{skill_name}' 初始化成功: {skill_dir}")
    print("\n下一步:")
    print("1. 编辑 SKILL.md 完成 TODO 占位项并更新 description")
    if resources:
        if include_examples:
            print("2. 自定义或删除 scripts/、references/、assets/ 中的示例文件")
        else:
            print("2. 按需添加资源到 scripts/、references/、assets/")
    else:
        print("2. 按需创建资源目录（scripts/、references/、assets/）")
    print("3. 使用 quick_validate.py 校验技能结构")

    return skill_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从模板创建新的技能目录。",
    )
    parser.add_argument("skill_name", help="技能名称（将规范化为 snake_case）")
    parser.add_argument("--path", required=True, help="技能输出目录")
    parser.add_argument(
        "--resources",
        default="",
        help="逗号分隔的资源类型: scripts,references,assets",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="在资源目录中创建示例文件",
    )
    args = parser.parse_args()

    raw_skill_name = args.skill_name
    skill_name = normalize_skill_name(raw_skill_name)
    if not skill_name:
        print("[ERROR] 技能名称必须包含至少一个字母或数字。")
        sys.exit(1)
    if len(skill_name) > MAX_SKILL_NAME_LENGTH:
        print(
            f"[ERROR] 技能名称 '{skill_name}' 过长 ({len(skill_name)} 字符)。"
            f" 最大允许 {MAX_SKILL_NAME_LENGTH} 字符。"
        )
        sys.exit(1)
    if skill_name != raw_skill_name:
        print(f"注意: 技能名称已从 '{raw_skill_name}' 规范化为 '{skill_name}'。")
    # 检查是否为 snake_case
    if not re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", skill_name):
        print(f"[ERROR] 技能名称 '{skill_name}' 不符合 snake_case 规范（仅小写字母、数字、下划线）。")
        sys.exit(1)

    resources = parse_resources(args.resources)
    if args.examples and not resources:
        print("[ERROR] --examples 需要 --resources 同时设置。")
        sys.exit(1)

    path = args.path

    print(f"初始化技能: {skill_name}")
    print(f"   位置: {path}")
    if resources:
        print(f"   资源: {', '.join(resources)}")
        if args.examples:
            print("   示例: 启用")
    else:
        print("   资源: 无（按需创建）")
    print()

    result = init_skill(skill_name, path, resources, args.examples)

    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
