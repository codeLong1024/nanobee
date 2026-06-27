"""
ContextSandbox 单元测试
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobee.kernel.sandbox import ContextSandbox
from nanobee.exceptions import SandboxViolationError


@pytest.fixture
def sandbox(tmp_path: Path) -> ContextSandbox:
    """创建沙箱，root 为 tmp_path/users/user-a"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True, exist_ok=True)
    return ContextSandbox(root)


# ====== resolve_safe ======


def test_resolve_safe_allowed(tmp_path: Path):
    """沙箱内路径正常返回"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    file_path = root / "memory" / "test.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.touch()

    result = sandbox.resolve_safe(str(file_path))
    assert result == file_path.resolve()


def test_resolve_safe_with_subdir(tmp_path: Path):
    """子目录路径正常"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    subdir = root / "sub" / "file.txt"
    subdir.parent.mkdir(parents=True, exist_ok=True)

    result = sandbox.resolve_safe(str(subdir))
    assert result == subdir.resolve()


def test_resolve_safe_escape_dotdot(tmp_path: Path):
    """.. 路径逃逸被拦截"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    escape_path = str(root / "../../user-b/secret.txt")

    with pytest.raises(SandboxViolationError) as exc_info:
        sandbox.resolve_safe(escape_path)
    assert "user-b" in str(exc_info.value)
    assert "路径超出沙箱" in str(exc_info.value)


def test_resolve_safe_unrelated_path(tmp_path: Path):
    """不相关的路径被拦截"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    other = tmp_path / "other" / "file.txt"

    with pytest.raises(SandboxViolationError):
        sandbox.resolve_safe(str(other))


def test_resolve_safe_root_itself(tmp_path: Path):
    """context_root 本身也是允许的"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    result = sandbox.resolve_safe(str(root))
    assert result == root.resolve()


# ====== sanitize_params (schema-driven) ======


def test_sanitize_params_path_field(tmp_path: Path):
    """清洗 path 参数（通过 x-constraint: sandbox）"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "test.txt"
    allowed.parent.mkdir(parents=True, exist_ok=True)

    schema = {"properties": {"path": {"x-constraint": "sandbox"}}}
    result = sandbox.sanitize_params("read_file", {"path": str(allowed)}, param_schema=schema)
    assert result["path"] == str(allowed.resolve())


def test_sanitize_params_directory_field(tmp_path: Path):
    """清洗 directory 参数（通过 x-constraint: sandbox）"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "subdir"
    allowed.mkdir(parents=True, exist_ok=True)

    schema = {"properties": {"directory": {"x-constraint": "sandbox"}}}
    result = sandbox.sanitize_params("list_dir", {"directory": str(allowed)}, param_schema=schema)
    assert result["directory"] == str(allowed.resolve())


def test_sanitize_params_escape_raises(tmp_path: Path):
    """越界路径在清洗时抛出异常（x-constraint: sandbox）"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    bad_path = str(root / "../../../etc/passwd")

    schema = {"properties": {"path": {"x-constraint": "sandbox"}}}
    with pytest.raises(SandboxViolationError):
        sandbox.sanitize_params("read_file", {"path": bad_path}, param_schema=schema)


def test_sanitize_params_non_path_untouched(tmp_path: Path):
    """非路径参数不会被修改（无 x-constraint 声明）"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    params = {"name": "hello", "count": 42, "flag": True}
    result = sandbox.sanitize_params("echo", params)
    assert result == params


def test_sanitize_params_no_schema_passthrough(tmp_path: Path):
    """无 param_schema 时所有参数直通（框架不猜测语义）"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    params = {"path": "/etc/passwd", "command": "ls"}
    result = sandbox.sanitize_params("some_tool", params)
    # 无 schema = 无约束，全部直通
    assert result == params


def test_sanitize_params_mixed(tmp_path: Path):
    """混合参数：有约束的清洗，无约束的直通"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    allowed = root / "f.txt"
    allowed.parent.mkdir(parents=True, exist_ok=True)

    params = {
        "text": "content",
        "path": str(allowed),
        "count": 10,
    }
    schema = {"properties": {"path": {"x-constraint": "writable"}}}
    result = sandbox.sanitize_params("write_file", params, param_schema=schema)
    assert result["text"] == "content"
    assert result["path"] == str(allowed.resolve())
    assert result["count"] == 10


def test_sanitize_params_workspace_constraint(tmp_path: Path):
    """x-constraint: workspace — 约束在 process_workspace 边界内"""
    root = tmp_path / "users" / "user-a"
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root, process_workspace=ws)

    # 在 workspace 内的路径通过
    inside_wd = ws / "subtask"
    inside_wd.mkdir(parents=True, exist_ok=True)
    schema = {"properties": {"working_dir": {"x-constraint": "workspace"}}}
    result = sandbox.sanitize_params("execute_shell", {"working_dir": str(inside_wd)}, param_schema=schema)
    assert result["working_dir"] == str(inside_wd.resolve())


def test_sanitize_params_workspace_escape_raises(tmp_path: Path):
    """x-constraint: workspace — 超出边界抛出异常"""
    root = tmp_path / "users" / "user-a"
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sandbox = ContextSandbox(root, process_workspace=ws)

    outside = root / "outside_wd"
    outside.mkdir(parents=True, exist_ok=True)
    schema = {"properties": {"working_dir": {"x-constraint": "workspace"}}}
    with pytest.raises(SandboxViolationError, match="进程工作目录边界"):
        sandbox.sanitize_params("execute_shell", {"working_dir": str(outside)}, param_schema=schema)


def test_sanitize_params_workspace_no_constraint_passthrough(tmp_path: Path):
    """无 process_workspace 设置时 workspace 约束不校验"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)  # 未设置 process_workspace

    schema = {"properties": {"working_dir": {"x-constraint": "workspace"}}}
    result = sandbox.sanitize_params("execute_shell", {"working_dir": "/tmp/somewhere"}, param_schema=schema)
    assert result["working_dir"] == str(Path("/tmp/somewhere").resolve())


# ====== assert_allowed ======


def test_assert_allowed_pass(tmp_path: Path):
    """断言通过"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    sandbox.assert_allowed(root / "memory")  # 不应抛出


def test_assert_allowed_fail(tmp_path: Path):
    """断言失败抛出 SandboxViolationError"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    with pytest.raises(SandboxViolationError):
        sandbox.assert_allowed("/etc")


# ====== properties ======


def test_context_root_property(tmp_path: Path):
    """context_root 属性正确"""
    root = tmp_path / "users" / "user-a"
    sandbox = ContextSandbox(root)
    assert sandbox.context_root == root.resolve()


def test_repr(tmp_path: Path):
    """repr 包含根路径"""
    root = tmp_path / "ctx"
    sandbox = ContextSandbox(root)
    rep = repr(sandbox)
    assert str(root.resolve()) in rep


# ====== 符号链接（symlink）攻击测试 ======


def test_resolve_safe_with_internal_symlink_to_external(tmp_path: Path):
    """内部符号链接指向沙箱外部 → 应抛 SandboxViolationError

    模拟攻击场景：攻击者在沙箱内创建指向 /etc/passwd 的符号链接，
    期望沙箱能通过 resolve() 解析到外部路径并拦截。
    """
    root = tmp_path / "users" / "user-a"
    (root / "subdir").mkdir(parents=True)

    # 沙箱外建一个文件，再在沙箱内创建指向它的符号链接
    external_file = tmp_path / "external_secret.txt"
    external_file.write_text("secret data", encoding="utf-8")
    symlink = root / "subdir" / "link_to_outside"
    symlink.symlink_to(external_file)

    sandbox = ContextSandbox(root)
    with pytest.raises(SandboxViolationError, match="路径超出沙箱允许范围"):
        sandbox.resolve_safe("subdir/link_to_outside")


def test_resolve_safe_with_external_symlink_to_internal(tmp_path: Path):
    """符号链接本身在沙箱外但指向沙箱内 → 应允许通过

    这不是攻击，而是验证 symlink 解析到沙箱内路径时能正常通过。
    """
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True)

    inside_file = root / "safe.txt"
    inside_file.write_text("safe content", encoding="utf-8")

    # 在沙箱外创建指向沙箱内文件的符号链接
    external_symlink = tmp_path / "outside_link"
    external_symlink.symlink_to(inside_file)

    sandbox = ContextSandbox(root)
    # 通过符号链接访问沙箱内文件，resolve() 后会落到沙箱内，应通过
    result = sandbox.resolve_safe(str(external_symlink))
    assert result == inside_file.resolve()


def test_resolve_safe_with_tricky_symlink(tmp_path: Path):
    """穿越多次 relative_to 仍是同级路径的 symlink"""
    root = tmp_path / "users" / "user-a"
    (root / "sub").mkdir(parents=True)

    # 沙箱外创建文件，沙箱内 symlink 指向它
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "sub" / "bad_link"
    link.symlink_to(outside)

    sandbox = ContextSandbox(root)
    with pytest.raises(SandboxViolationError, match="路径超出沙箱允许范围"):
        sandbox.resolve_safe("sub/bad_link")


def test_context_root_is_symlink(tmp_path: Path):
    """context_root 本身是符号链接 → __init__ 中已 resolve，不影响后续检查"""
    real_root = tmp_path / "real_context" / "user-a"
    real_root.mkdir(parents=True)

    symlink_root = tmp_path / "symlink_root"
    symlink_root.symlink_to(real_root)

    sandbox = ContextSandbox(symlink_root)
    # context_root 属性应返回解析后的真实路径
    assert sandbox.context_root == real_root.resolve()

    # 沙箱内操作应基于真实路径正常工作（使用绝对路径）
    inside = real_root / "file.txt"
    inside.write_text("hello", encoding="utf-8")
    result = sandbox.resolve_safe(str(inside))
    assert result == inside.resolve()


def test_assert_allowed_with_symlink(tmp_path: Path):
    """assert_allowed 同样能拦截指向外部的符号链接"""
    root = tmp_path / "users" / "user-a"
    root.mkdir(parents=True)

    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "evil_link"
    link.symlink_to(outside)

    sandbox = ContextSandbox(root)
    with pytest.raises(SandboxViolationError, match="路径越界断言失败"):
        sandbox.assert_allowed(link)


# ====== 多根白名单测试（read_only_roots） ======


def test_resolve_safe_read_only_root_allowed(tmp_path: Path):
    """绝对路径在只读根内，resolve_safe 应允许通过"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    # 只读根内的文件
    skill_file = readonly / "test_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("content", encoding="utf-8")

    result = sandbox.resolve_safe(str(skill_file))
    assert result == skill_file.resolve()


def test_resolve_safe_writable_in_read_only_blocked(tmp_path: Path):
    """resolve_safe_writable 对只读根内的路径应抛出"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    skill_file = readonly / "test_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)

    with pytest.raises(SandboxViolationError, match="写入路径逃逸拦截"):
        sandbox.resolve_safe_writable(str(skill_file))


def test_resolve_safe_writable_in_writable_allowed(tmp_path: Path):
    """resolve_safe_writable 对可写根内的路径应允许"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    target = writable / "memory" / "facts.md"
    target.parent.mkdir(parents=True)

    result = sandbox.resolve_safe_writable(str(target))
    assert result == target.resolve()


def test_assert_allowed_with_read_only_root(tmp_path: Path):
    """assert_allowed 对只读根内的路径应通过"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    skill_file = readonly / "some_skill"
    skill_file.mkdir(parents=True)

    # 不抛出
    sandbox.assert_allowed(skill_file)


def test_assert_allowed_writable_in_read_only_blocked(tmp_path: Path):
    """assert_allowed_writable 对只读根内路径应抛出"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    skill_file = readonly / "some_skill"
    skill_file.mkdir(parents=True)

    with pytest.raises(SandboxViolationError, match="写入路径断言失败"):
        sandbox.assert_allowed_writable(skill_file)


def test_assert_allowed_writable_in_writable_allowed(tmp_path: Path):
    """assert_allowed_writable 对可写根内路径应通过"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    target = writable / "skills" / "my_skill"
    target.mkdir(parents=True)

    # 不抛出
    sandbox.assert_allowed_writable(target)


def test_sanitize_params_with_read_only_root(tmp_path: Path):
    """sanitize_params 对只读根内的路径应正常解析（x-constraint: sandbox）"""
    writable = tmp_path / "users" / "user-a"
    writable.mkdir(parents=True)
    readonly = tmp_path / "skills"
    readonly.mkdir(parents=True)

    sandbox = ContextSandbox(writable, read_only_roots=[readonly])

    skill_file = readonly / "test_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)

    schema = {"properties": {"path": {"x-constraint": "sandbox"}}}
    result = sandbox.sanitize_params("read_file", {"path": str(skill_file)}, param_schema=schema)
    assert result["path"] == str(skill_file.resolve())


def test_repr_with_read_only_roots(tmp_path: Path):
    """repr 包含只读根信息"""
    writable = tmp_path / "ctx"
    readonly = tmp_path / "builtin"
    sandbox = ContextSandbox(writable, read_only_roots=[readonly])
    rep = repr(sandbox)
    assert "writable" in rep
    assert "read_only" in rep


def test_repr_with_process_workspace(tmp_path: Path):
    """repr 包含 process_workspace 信息"""
    writable = tmp_path / "ctx"
    ws = writable / "workspace"
    sandbox = ContextSandbox(writable, process_workspace=ws)
    rep = repr(sandbox)
    assert "process_workspace" in rep
    assert str(ws.resolve()) in rep


def test_no_read_only_roots_repr_stable(tmp_path: Path):
    """无只读根和 process_workspace 时 repr 简化为 writable 格式"""
    root = tmp_path / "ctx"
    sandbox = ContextSandbox(root)
    rep = repr(sandbox)
    assert str(root.resolve()) in rep
    assert "ContextSandbox" in rep
