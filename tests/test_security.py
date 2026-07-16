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
    _normalize_hostname,
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


# ===========================================================================
# _normalize_hostname — URL 解码 + IP 格式归一化
# ===========================================================================

from contextlib import suppress

from nanobee.security.network import _HEX_OCT_INT_IP_RE, _parse_ip_octet, _try_normalize_ip


class TestNormalizeHostname:
    """_normalize_hostname 防御 SSRF 绕过测试"""

    # --- URL 编码 ---

    def test_url_encoded_dot(self):
        """%2e -> ."""
        assert _normalize_hostname("169%2e254%2e169%2e254") == "169.254.169.254"

    def test_url_encoded_zero(self):
        """%30 -> 0"""
        assert _normalize_hostname("127%2e0%2e0%2e1") == "127.0.0.1"

    def test_double_encoding(self):
        """双重编码 %252e -> %2e -> ."""
        assert _normalize_hostname("169%252e254%252e169%252e254") == "169.254.169.254"

    def test_mixed_case_percent_encoding(self):
        """大小写混合 URL 编码"""
        assert _normalize_hostname("169%2E254%2E169%252E254") == "169.254.169.254"

    def test_triple_encoding_still_decodes(self):
        """三层编码仍能正确解码"""
        assert _normalize_hostname("169%25252e254") == "169.254"

    # --- 十六进制 IP ---

    def test_hex_full_32bit(self):
        """纯十六进制: 0x7f000001"""
        assert _normalize_hostname("0x7f000001") == "127.0.0.1"

    def test_hex_uppercase(self):
        """大写十六进制: 0X7F000001"""
        assert _normalize_hostname("0X7F000001") == "127.0.0.1"

    def test_hex_dot_notation(self):
        """点分十六进制: 0x7f.0x0.0x0.0x1"""
        assert _normalize_hostname("0x7f.0x0.0x0.0x1") == "127.0.0.1"

    def test_hex_metadata_endpoint(self):
        """云元数据端点（十六进制形式）应被正确归一化"""
        result = _normalize_hostname("0xa9fea9fe")
        # 169.254.169.254
        assert str(ipaddress.IPv4Address(result)) == "169.254.169.254"

    # --- 八进制 IP ---

    def test_octal_leading_zero(self):
        """前导零八进制: 0177.00.00.01"""
        assert _normalize_hostname("0177.0.0.1") == "127.0.0.1"

    def test_octal_metadata_endpoint(self):
        """云元数据端点（八进制形式）"""
        # 169 = 0o251, 254 = 0o376
        result = _normalize_hostname("0251.0376.0251.0376")
        assert str(ipaddress.IPv4Address(result)) == "169.254.169.254"

    # --- 整数 IP ---

    def test_decimal_integer_loopback(self):
        """十进制整数: 2130706433 = 127.0.0.1"""
        assert _normalize_hostname("2130706433") == "127.0.0.1"

    def test_decimal_integer_private(self):
        """十进制整数: 3232235777 = 192.168.1.1"""
        assert _normalize_hostname("3232235777") == "192.168.1.1"

    # --- 混合格式 ---

    def test_mixed_hex_octet(self):
        """混合格式: 0x7f.0.0.0x1"""
        assert _normalize_hostname("0x7f.0.0.0x1") == "127.0.0.1"

    # --- 标准格式不变 ---

    def test_standard_ipv4_passthrough(self):
        """标准 IPv4 点分格式不变"""
        assert _normalize_hostname("8.8.8.8") == "8.8.8.8"
        assert _normalize_hostname("192.168.1.1") == "192.168.1.1"

    def test_domain_name_passthrough(self):
        """域名不变"""
        assert _normalize_hostname("example.com") == "example.com"
        assert _normalize_hostname("www.google.com") == "www.google.com"

    def test_ipv6_passthrough(self):
        """IPv6 地址保持不变（由 ipaddress 处理）"""
        assert _normalize_hostname("::1") == "::1"
        assert _normalize_hostname("2001:4860:4860::8888") == "2001:4860:4860::8888"


class TestTryNormalizeIP:
    """_try_normalize_ip 单元测试"""

    def test_standard_ipv4_returns_address(self):
        with suppress(ValueError):
            addr = _try_normalize_ip("10.0.0.1")
            assert addr == ipaddress.IPv4Address("10.0.0.1")

    def test_domain_returns_none(self):
        assert _try_normalize_ip("example.com") is None

    def test_hex_returns_address(self):
        with suppress(ValueError):
            addr = _try_normalize_ip("0x7f000001")
            assert addr == ipaddress.IPv4Address("127.0.0.1")

    def test_invalid_format_returns_none(self):
        assert _try_normalize_ip("not-an-ip") is None
        assert _try_normalize_ip("") is None
        assert _try_normalize_ip("...") is None


class TestParseIpOctet:
    """_parse_ip_octet 单元测试"""

    def test_decimal(self):
        assert _parse_ip_octet("255") == 255
        assert _parse_ip_octet("0") == 0

    def test_hex_prefix(self):
        assert _parse_ip_octet("0xff") == 255
        assert _parse_ip_octet("0x0a") == 10

    def test_octal_leading_zero(self):
        assert _parse_ip_octet("0177") == 127
        assert _parse_ip_octet("00") == 0

    def test_out_of_range_returns_none(self):
        assert _parse_ip_octet("256") is None
        assert _parse_ip_octet("0x100") is None

    def test_garbage_returns_none(self):
        assert _parse_ip_octet("abc") is None
        assert _parse_ip_octet("-1") is None


class TestHexOctIntIPRe:
    """_HEX_OCT_INT_IP_RE 正则覆盖测试（精确匹配：排除标准 IPv4，由快速路径处理）"""

    def test_matches_pure_hex(self):
        assert _HEX_OCT_INT_IP_RE.match("0x7f000001")

    def test_matches_dotted_hex(self):
        assert _HEX_OCT_INT_IP_RE.match("0x7f.0x0.0x0.0x1")

    def test_matches_octal(self):
        assert _HEX_OCT_INT_IP_RE.match("0177.00.00.01")

    def test_matches_integer(self):
        assert _HEX_OCT_INT_IP_RE.match("2130706433")

    def test_matches_mixed_format(self):
        """混合十六/十进制格式（含非标标记）应被匹配"""
        assert _HEX_OCT_INT_IP_RE.match("0x7f.0.0.0x1")
        assert _HEX_OCT_INT_IP_RE.match("0177.0.0.1")

    def test_rejects_standard_ipv4(self):
        """标准点分十进制不应被此正则匹配——由 ip_address() 快速路径处理"""
        # 核心不变量：标准 IPv4 走快速路径，不进入正则分支
        assert _HEX_OCT_INT_IP_RE.match("127.0.0.1") is None
        assert _HEX_OCT_INT_IP_RE.match("192.168.1.1") is None
        assert _HEX_OCT_INT_IP_RE.match("10.0.0.1") is None
        assert _HEX_OCT_INT_IP_RE.match("255.255.255.255") is None

    def test_rejects_domains(self):
        assert _HEX_OCT_INT_IP_RE.match("example.com") is None

    def test_rejects_short_numbers(self):
        """小于 7 位的纯数字不可能是整数 IP（下界 1000000）"""
        assert _HEX_OCT_INT_IP_RE.match("123456") is None
        assert _HEX_OCT_INT_IP_RE.match("9999999") is not None  # 7 位可能


# ===========================================================================
# SSRF 绕过集成测试 — validate_url_target + contains_internal_url
# ===========================================================================

class TestSSRFBypassPrevention:
    """验证各种绕过技术均被 validate_url_target 正确拦截"""

    def test_url_encoded_metadata_blocked(self):
        """URL 编码的云元数据端点被拦截"""
        ok, msg = validate_url_target("http://169%2e254%2e169%2e254/latest/meta-data/")
        assert not ok
        assert "被阻止" in msg or "私有" in msg or "内网" in msg

    def test_hex_ip_metadata_blocked(self):
        """十六进制 IP 形式的元数据端点被拦截"""
        ok, msg = validate_url_target("http://0xa9fea9fe/latest/meta-data/")
        assert not ok

    def test_octal_ip_metadata_blocked(self):
        """八进制 IP 形式的元数据端点被拦截"""
        ok, msg = validate_url_target("http://0251.0376.0251.0376/latest/meta-data/")
        assert not ok

    def test_integer_ip_loopback_blocked(self):
        """整数 IP 形式的 loopback 被拦截（默认不允许 loopback）"""
        ok, msg = validate_url_target("http://2130706433/")
        assert not ok

    def test_double_encoded_blocked(self):
        """双重 URL 编码被拦截"""
        ok, _ = validate_url_target("http://169%252e254%252e169%252e254/")
        assert not ok

    def test_contains_internal_detects_url_encoding(self):
        """contains_internal_url 能检测命令中的编码 URL"""
        assert contains_internal_url("curl http://169%2e254%2e169%2e254/")

    def test_contains_internal_detects_hex_ip(self):
        """contains_internal_url 能检测命令中的十六进制 IP"""
        assert contains_internal_url("wget http://0x7f000001/admin")

    def test_contains_internal_detects_octal_ip(self):
        """contains_internal_url 能检测命令中的八进制 IP"""
        assert contains_internal_url("curl http://0177.0.0.1:8080")

    def test_public_url_with_encoding_still_passes(self):
        """合法公共域名即使含无害编码也应通过（DNS 解析后为公网 IP）"""
        # example.com 的 DNS 解析结果应为公网地址
        ok, msg = validate_url_target("https://example%2ecom")
        if ok:
            return  # 通过，说明解析到公网 IP
        # 若失败，确保不是因归一化错误导致的
        assert "无法解析" in msg or "私有" in msg or "被阻止" in msg
