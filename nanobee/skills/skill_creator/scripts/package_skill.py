#!/usr/bin/env python3
"""
技能打包器 —— 将技能目录打包为可分发的 .skill 文件。

用法:
    python package_skill.py <path/to/skill-folder> [output-directory]

示例:
    python package_skill.py contexts/user_001/skills/my_skill
    python package_skill.py contexts/user_001/skills/my_skill ./dist
"""

import sys
import zipfile
from contextlib import suppress
from pathlib import Path

from quick_validate import validate_skill


def _is_within(path: Path, root: Path) -> bool:
    with suppress(ValueError):
        path.relative_to(root)
        return True
    return False


def _cleanup_partial_archive(skill_filename: Path) -> None:
    if skill_filename.exists():
        with suppress(OSError):
            skill_filename.unlink()


def package_skill(skill_path: str, output_dir: str | None = None) -> Path | None:
    """将技能文件夹打包为 .skill 文件。

    Args:
        skill_path: 技能文件夹路径
        output_dir: 可选的输出目录（默认为当前目录）

    Returns:
        创建的 .skill 文件路径，或 None（失败时）
    """
    skill_path = Path(skill_path).resolve()

    # 验证技能文件夹
    if not skill_path.exists():
        print(f"[ERROR] 技能文件夹不存在: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"[ERROR] 路径不是目录: {skill_path}")
        return None

    # 验证 SKILL.md 存在
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"[ERROR] SKILL.md 不存在: {skill_path}")
        return None

    # 打包前运行校验
    print("校验技能...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"[ERROR] 校验失败: {message}")
        print("   请修复校验错误后重新打包。")
        return None
    print(f"[OK] {message}\n")

    # 确定输出位置
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"

    EXCLUDED_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules"}

    files_to_package: list[Path] = []
    resolved_archive = skill_filename.resolve()

    for file_path in skill_path.rglob("*"):
        # 拒绝符号链接
        if file_path.is_symlink():
            print(f"[ERROR] 技能包中不允许符号链接: {file_path}")
            _cleanup_partial_archive(skill_filename)
            return None

        rel_parts = file_path.relative_to(skill_path).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue

        if file_path.is_file():
            resolved_file = file_path.resolve()
            if not _is_within(resolved_file, skill_path):
                print(f"[ERROR] 文件逃逸了技能根目录: {file_path}")
                _cleanup_partial_archive(skill_filename)
                return None
            # 避免将输出包自身打包进去
            if resolved_file == resolved_archive:
                print(f"[WARN] 跳过输出归档: {file_path}")
                continue
            files_to_package.append(file_path)

    # 创建 .skill 文件（Zip 格式）
    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in files_to_package:
                # 在 zip 中保持技能名作为顶级目录
                arcname = Path(skill_name) / file_path.relative_to(skill_path)
                zipf.write(file_path, arcname)
                print(f"  添加: {arcname}")

        print(f"\n[OK] 成功打包技能为: {skill_filename}")
        return skill_filename

    except Exception as e:
        _cleanup_partial_archive(skill_filename)
        print(f"[ERROR] 创建 .skill 文件失败: {e}")
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python package_skill.py <path/to/skill-folder> [output-directory]")
        print("\n示例:")
        print("  python package_skill.py contexts/user_001/skills/my_skill")
        print("  python package_skill.py contexts/user_001/skills/my_skill ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"打包技能: {skill_path}")
    if output_dir:
        print(f"   输出目录: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
