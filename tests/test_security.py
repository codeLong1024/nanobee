"""安全模块测试 — SSRF 防护 + 路径边界工具"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

import pytest

from nanobee.security.network import (
    SSRF_BOUNDARY_NOTE,
    _is_private,
    _normalize_addr,
    configure_ssrf_whitelist,
    contains_internal_url,
    validate_resolved_url,
    validate_url_target,
)
from nanobee.security.workspace_policy import (
    WORKSPACE_BOUNDARY_NOTE,
    is_path_allowed,
    is_path_within,
    require_path_within,
    resolve_allowed_path,
    resolve_path,
)
from nanobee.exceptions import SandboxViolationError


# ===========================================================================
# _normalize_addr
# ===========================================================================

class TestNormalizeAddr:
    def test_ipv4_passthrough(self):
        addr = ipaddress.IPv4Address("127.0.0.1")
        assert _normalize_addr(addr) is addr

    def test_ipv6_mapped_to_ipv4(self):
        v6 = ipaddress.IPv6Address("::ffff:127.0.0.1")
        result = _normalize_addr(v6)
        assert isinstance(result, ipaddress.IPv4Address)
        assert str(result) == "127.0.0.1"

    def test_ipv6_not_mapped(self):
        v6 = ipaddress.IPv6Address("::1")
        assert _normalize_addr(v6) is v6

    def test_ipv6_mapped_rfc1918(self):
        v6 = ipaddress.IPv6Address("::ffff:192.168.1.1")
        result = _normalize_addr(v6)
        assert isinstance(result, ipaddress.IPv4Address)
        assert str(result) == "192.168.1.1"


# ===========================================================================
# _is_private
# ===========================================================================

class TestIsPrivate:
    def test_rfc1918(self):
        assert _is_private(ipaddress.IPv4Address("10.0.0.1"))
        assert _is_private(ipaddress.IPv4Address("172.16.0.1"))
        assert _is_private(ipaddress.IPv4Address("192.168.1.1"))

    def test_loopback(self):
        assert _is_private(ipaddress.IPv4Address("127.0.0.1"))

    def test_link_local(self):
        assert _is_private(ipaddress.IPv4Address("169.254.169.254"))

    def test_carrier_nat(self):
        assert _is_private(ipaddress.IPv4Address("100.64.0.1"))

    def test_public_ip_not_private(self):
        assert not _is_private(ipaddress.IPv4Address("8.8.8.8"))
        assert not _is_private(ipaddress.IPv4Address("1.1.1.1"))
        assert not _is_private(ipaddress.IPv4Address("142.250.80.46"))

    def test_ipv6_loopback(self):
        assert _is_private(ipaddress.IPv6Address("::1"))

    def test_ipv6_unique_local(self):
        assert _is_private(ipaddress.IPv6Address("fc00::1"))

    def test_ipv6_link_local(self):
        assert _is_private(ipaddress.IPv6Address("fe80::1"))

    def test_ipv6_public_not_private(self):
        assert not _is_private(ipaddress.IPv6Address("2001:4860:4860::8888"))

    def test_whitelist_bypasses(self):
        """白名单中的 CIDR 不被视为私有"""
        configure_ssrf_whitelist(["100.64.0.0/10"])
        try:
            assert not _is_private(ipaddress.IPv4Address("100.64.0.1"))
            # RFC1918 仍然被阻止
            assert _is_private(ipaddress.IPv4Address("10.0.0.1"))
        finally:
            configure_ssrf_whitelist([])

    def test_ipv6_mapped_rfc1918(self):
        """IPv6-mapped IPv4 应匹配 IPv4 掩码"""
        v6 = ipaddress.IPv6Address("::ffff:10.0.0.1")
        assert _is_private(v6)

    def test_ipv6_mapped_public(self):
        v6 = ipaddress.IPv6Address("::ffff:8.8.8.8")
        assert not _is_private(v6)


# ===========================================================================
# validate_url_target — 需要网络/DNS
# ===========================================================================

class TestValidateUrlTarget:
    def test_invalid_scheme(self):
        ok, msg = validate_url_target("ftp://example.com")
        assert not ok
        assert "仅允许" in msg

    def test_missing_domain(self):
        ok, msg = validate_url_target("http://")
        assert not ok
        assert "缺少域名" in msg or "缺少主机名" in msg

    def test_empty_url(self):
        ok, msg = validate_url_target("")
        assert not ok

    def test_public_url(self):
        """公共 URL 应该通过验证（需要 DNS）"""
        ok, msg = validate_url_target("https://example.com")
        assert ok, f"公共 URL 应通过: {msg}"

    def test_google_public(self):
        ok, msg = validate_url_target("https://www.google.com")
        assert ok, f"Google 应通过: {msg}"

    def test_private_ip_loopback_fails(self):
        """私有 IP 作为 URL 主机名应被阻止"""
        ok, msg = validate_url_target("http://127.0.0.1")
        assert not ok
        assert "被阻止" in msg or "私有" in msg

    def test_rfc1918_rejected(self):
        ok, msg = validate_url_target("http://10.0.0.1")
        assert not ok
        assert "被阻止" in msg or "私有" in msg

    def test_metadata_rejected(self):
        ok, msg = validate_url_target("http://169.254.169.254")
        assert not ok
        assert "被阻止" in msg or "私有" in msg

    def test_loopback_rejected(self):
        ok, msg = validate_url_target("http://192.168.1.1")
        assert not ok
        assert "被阻止" in msg or "私有" in msg

    def test_allow_loopback(self):
        ok, msg = validate_url_target("http://127.0.0.1", allow_loopback=True)
        assert ok, f"允许 loopback 时应通过: {msg}"

    def test_allow_loopback_localhost(self):
        ok, msg = validate_url_target("http://localhost", allow_loopback=True)
        assert ok, f"localhost 应通过: {msg}"


# ===========================================================================
# validate_resolved_url
# ===========================================================================

class TestValidateResolvedUrl:
    def test_private_ip_rejected(self):
        ok, msg = validate_resolved_url("http://192.168.1.1")
        assert not ok
        assert "私有地址" in msg

    def test_public_domain_accept(self):
        ok, msg = validate_resolved_url("https://example.com")
        assert ok, f"公共域名应通过: {msg}"

    def test_no_hostname_accept(self):
        ok, _ = validate_resolved_url("/relative/path")
        assert ok

    def test_invalid_url_accept(self):
        ok, _ = validate_resolved_url("")
        assert ok


# ===========================================================================
# contains_internal_url
# ===========================================================================

class TestContainsInternalUrl:
    def test_plain_command_no_url(self):
        assert not contains_internal_url("ls -la /tmp")

    def test_public_url_not_internal(self):
        assert not contains_internal_url("curl https://example.com/data")

    def test_internal_ip_url_detected(self):
        assert contains_internal_url("curl http://169.254.169.254/")
        assert contains_internal_url("wget http://10.0.0.1/config")
        assert contains_internal_url("http://192.168.1.1/admin")

    def test_loopback_detected(self):
        assert contains_internal_url("curl http://127.0.0.1:8080")

    def test_allow_loopback(self):
        assert not contains_internal_url("curl http://127.0.0.1", allow_loopback=True)

    def test_multiple_urls_partial_internal(self):
        assert contains_internal_url("curl https://example.com http://10.0.0.1")


# ===========================================================================
# configure_ssrf_whitelist
# ===========================================================================

class TestConfigureSSRFWhitelist:
    def test_whitelist_clears_private(self):
        configure_ssrf_whitelist(["10.0.0.0/8"])
        try:
            assert not _is_private(ipaddress.IPv4Address("10.0.0.1"))
            # 但其他私有地址仍然被阻止
            assert _is_private(ipaddress.IPv4Address("192.168.1.1"))
        finally:
            configure_ssrf_whitelist([])

    def test_invalid_cidr_ignored(self):
        configure_ssrf_whitelist(["not-a-cidr"])
        try:
            # 白名单应为空，所有私有地址仍被阻止
            assert _is_private(ipaddress.IPv4Address("10.0.0.1"))
        finally:
            configure_ssrf_whitelist([])

    def test_reset_on_empty(self):
        configure_ssrf_whitelist(["10.0.0.0/8"])
        configure_ssrf_whitelist([])
        assert _is_private(ipaddress.IPv4Address("10.0.0.1"))

    def test_carrier_grade_nat_whitelisted(self):
        configure_ssrf_whitelist(["100.64.0.0/10"])
        try:
            assert not _is_private(ipaddress.IPv4Address("100.64.0.1"))
        finally:
            configure_ssrf_whitelist([])


# ===========================================================================
# SSRF_BOUNDARY_NOTE
# ===========================================================================

class TestSSRFBoundaryNote:
    def test_note_non_empty(self):
        assert SSRF_BOUNDARY_NOTE
        assert "non-bypassable" in SSRF_BOUNDARY_NOTE
        assert "ssrfWhitelist" in SSRF_BOUNDARY_NOTE


# ===========================================================================
# workspace_policy — resolve_path
# ===========================================================================

class TestResolvePath:
    def test_absolute_passthrough(self):
        result = resolve_path("/tmp")
        assert result == Path("/tmp").resolve()

    def test_relative_with_workspace(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        result = resolve_path("sub", workspace=tmp_path)
        assert result == sub.resolve()

    def test_relative_without_workspace(self):
        """无 workspace 时相对路径基于 CWD"""
        result = resolve_path(".")
        assert result.is_absolute()


# ===========================================================================
# workspace_policy — is_path_within
# ===========================================================================

class TestIsPathWithin:
    def test_same_path(self, tmp_path: Path):
        assert is_path_within(tmp_path, tmp_path)

    def test_child_path(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert is_path_within(sub, tmp_path)

    def test_outside_path(self, tmp_path: Path):
        other = Path("/tmp")
        assert not is_path_within(other, tmp_path)

    def test_deep_nested(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        assert is_path_within(deep, tmp_path)


# ===========================================================================
# workspace_policy — is_path_allowed
# ===========================================================================

class TestIsPathAllowed:
    def test_single_root(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert is_path_allowed(sub, [tmp_path])

    def test_any_root_matches(self, tmp_path: Path):
        other = Path("/tmp")
        assert is_path_allowed(tmp_path, [other, tmp_path])

    def test_no_roots(self, tmp_path: Path):
        assert not is_path_allowed(tmp_path, [])


# ===========================================================================
# workspace_policy — require_path_within
# ===========================================================================

class TestRequirePathWithin:
    def test_path_inside_succeeds(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        result = require_path_within(sub, tmp_path)
        assert result == sub.resolve()

    def test_path_outside_raises(self, tmp_path: Path):
        with pytest.raises(SandboxViolationError):
            require_path_within("/tmp", tmp_path)

    def test_custom_message(self, tmp_path: Path):
        with pytest.raises(SandboxViolationError, match="自定义错误"):
            require_path_within("/tmp", tmp_path, message="自定义错误")


# ===========================================================================
# workspace_policy — resolve_allowed_path
# ===========================================================================

class TestResolveAllowedPath:
    def test_no_allowed_root(self, tmp_path: Path):
        """无 allowed_root 时直接解析"""
        result = resolve_allowed_path("/tmp")
        assert result == Path("/tmp").resolve()

    def test_path_inside_succeeds(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        result = resolve_allowed_path(str(sub), allowed_root=tmp_path)
        assert result == sub.resolve()

    def test_path_outside_raises(self, tmp_path: Path):
        with pytest.raises(SandboxViolationError):
            resolve_allowed_path("/tmp", allowed_root=tmp_path)

    def test_relative_path_with_workspace(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        result = resolve_allowed_path("sub", workspace=tmp_path, allowed_root=tmp_path)
        assert result == sub.resolve()

    def test_extra_allowed_roots(self, tmp_path: Path):
        extra = Path("/tmp")
        result = resolve_allowed_path("/tmp", extra_allowed_roots=[extra])
        assert result == extra.resolve()


# ===========================================================================
# WORKSPACE_BOUNDARY_NOTE
# ===========================================================================

class TestWorkSpaceBoundaryNote:
    def test_note_non_empty(self):
        assert WORKSPACE_BOUNDARY_NOTE
        assert "硬性策略边界" in WORKSPACE_BOUNDARY_NOTE
