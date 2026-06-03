"""
ContextSandbox 单元测试
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.kernel.sandbox import ContextSandbox, SandboxError


@pytest.fixture
def sandbox(tmp_path: Path) -> ContextSandbox:
    """创建沙箱，root 为 tmp_path/contexts/user-a"""
    root = tmp_path / "contexts" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    return ContextSandbox(root)


# ====== resolve_safe ======


def test_resolve_safe_allowed(tmp_path: Path):
    """沙箱内路径正常返回"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    file_path = root / "memory" / "test.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.touch()

    result = sandbox.resolve_safe(str(file_path))
    assert result == file_path.resolve()


def test_resolve_safe_with_subdir(tmp_path: Path):
    """子目录路径正常"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    subdir = root / "work" / "sub" / "file.txt"
    subdir.parent.mkdir(parents=True, exist_ok=True)

    result = sandbox.resolve_safe(str(subdir))
    assert result == subdir.resolve()


def test_resolve_safe_escape_dotdot(tmp_path: Path):
    """.. 路径逃逸被拦截"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    escape_path = str(root / "../../user-b/secret.txt")

    with pytest.raises(SandboxError) as exc_info:
        sandbox.resolve_safe(escape_path)
    assert "user-b" in str(exc_info.value)
    assert "路径逃逸" in str(exc_info.value)


def test_resolve_safe_unrelated_path(tmp_path: Path):
    """不相关的路径被拦截"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    other = tmp_path / "other" / "file.txt"

    with pytest.raises(SandboxError):
        sandbox.resolve_safe(str(other))


def test_resolve_safe_root_itself(tmp_path: Path):
    """context_root 本身也是允许的"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    result = sandbox.resolve_safe(str(root))
    assert result == root.resolve()


# ====== sanitize_params ======


def test_sanitize_params_path_field(tmp_path: Path):
    """清洗 path 参数"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "work" / "test.txt"
    allowed.parent.mkdir(parents=True, exist_ok=True)

    result = sandbox.sanitize_params("read_file", {"path": str(allowed)})
    assert result["path"] == str(allowed.resolve())


def test_sanitize_params_directory_field(tmp_path: Path):
    """清洗 directory 参数"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "work" / "subdir"
    allowed.mkdir(parents=True, exist_ok=True)

    result = sandbox.sanitize_params("list_dir", {"directory": str(allowed)})
    assert result["directory"] == str(allowed.resolve())


def test_sanitize_params_escape_raises(tmp_path: Path):
    """越界路径在清洗时抛出异常"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    bad_path = str(root / "../../../etc/passwd")

    with pytest.raises(SandboxError):
        sandbox.sanitize_params("read_file", {"path": bad_path})


def test_sanitize_params_non_path_untouched(tmp_path: Path):
    """非路径参数不会被修改"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    params = {"name": "hello", "count": 42, "flag": True}
    result = sandbox.sanitize_params("echo", params)
    assert result == params


def test_sanitize_params_mixed(tmp_path: Path):
    """混合参数：路径被清洗，非路径不变"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "work" / "f.txt"
    allowed.parent.mkdir(parents=True, exist_ok=True)

    params = {
        "text": "content",
        "path": str(allowed),
        "count": 10,
    }
    result = sandbox.sanitize_params("write_file", params)
    assert result["text"] == "content"
    assert result["path"] == str(allowed.resolve())
    assert result["count"] == 10


def test_sanitize_params_working_dir(tmp_path: Path):
    """working_dir 参数被清洗"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    wd = root / "work"
    wd.mkdir(parents=True, exist_ok=True)

    result = sandbox.sanitize_params("exec", {"working_dir": str(wd)})
    assert result["working_dir"] == str(wd.resolve())


# ====== assert_allowed ======


def test_assert_allowed_pass(tmp_path: Path):
    """断言通过"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    sandbox.assert_allowed(root / "memory")  # 不应抛出


def test_assert_allowed_fail(tmp_path: Path):
    """断言失败抛出 SandboxError"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    with pytest.raises(SandboxError):
        sandbox.assert_allowed("/etc")


# ====== properties ======


def test_context_root_property(tmp_path: Path):
    """context_root 属性正确"""
    root = tmp_path / "contexts" / "user-a"
    sandbox = ContextSandbox(root)
    assert sandbox.context_root == root.resolve()


def test_repr(tmp_path: Path):
    """repr 包含根路径"""
    root = tmp_path / "ctx"
    sandbox = ContextSandbox(root)
    rep = repr(sandbox)
    assert str(root.resolve()) in rep
