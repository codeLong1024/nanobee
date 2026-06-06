"""网络安全工具 — SSRF 防护与内网 URL 检测

移植自 nanobot/security/network.py（MIT License，保留上游版权）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

# 非可绕过的 SSRF 策略边界提示
SSRF_BOUNDARY_NOTE = (
    "This is a non-bypassable security boundary. Stop trying to access "
    "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
    "alternate DNS, redirects, proxies, or another tool. Ask the user for "
    "local files, logs, screenshots, or an explicit safe public URL instead. "
    "If the user explicitly trusts this private URL, ask them to whitelist "
    "the exact IP/CIDR via tools.ssrfWhitelist."
)

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """配置 SSRF 白名单 CIDR 范围。

    白名单中的 CIDR 将绕过 SSRF 私有地址检查（如 Tailscale 的 ``100.64.0.0/10``）。

    Args:
        cidrs: CIDR 字符串列表
    """
    global _allowed_networks
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """标准化 IPv6-mapped IPv4 地址为其 IPv4 形式。

    ``::ffff:127.0.0.1`` 在语义上等同于 ``127.0.0.1``，
    但 Python 的 ipaddress 将其视为 IPv6Address，既不属于 ``127.0.0.0/8``
    也不属于 ``::1/128``。转换为 IPv4 可确保黑/白名单检查正确。
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 地址是否为私有/内网地址。

    优先匹配白名单，白名单中的地址不被视为私有。
    """
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """验证 URL 是否安全可获取：检查 scheme、主机名和解析后的 IP 地址。

    ``allow_loopback`` 有意保持狭窄：仅当所有解析地址都是 loopback 时，
    才允许 literal loopback 主机（localhost、127.0.0.0/8、::1）。
    不允许 RFC1918、link-local、元数据端点或解析到 loopback 的公共 DNS 名称。

    Returns:
        (ok, error_message)。ok 为 True 时 error_message 为空。
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"仅允许 http/https，实际为 '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "缺少域名"

    hostname = p.hostname
    if not hostname:
        return False, "缺少主机名"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"无法解析主机名: {hostname}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, ""
    for addr in addrs:
        if _is_private(addr):
            return False, f"被阻止: {hostname} 解析为私有/内网地址 {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """验证已获取的 URL（如重定向后）。仅检查 IP，跳过 DNS 解析。

    用于重定向场景：重定向后的 URL 可能不含主机名（如相对路径），
    此时返回 True（让调用方自行处理）。
    """
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            return False, f"重定向目标为私有地址: {addr}"
    except ValueError:
        # hostname 是域名，需要解析
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"重定向目标 {hostname} 解析到私有地址 {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """检查命令字符串中是否包含指向内网/私有地址的 URL。

    用于 shell 命令的安全守卫——检测 ``curl http://169.254.169.254/`` 等。

    Args:
        command: 命令字符串
        allow_loopback: 是否允许 loopback 地址

    Returns:
        True 如果命令包含内网 URL
    """
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    """检查主机名是否被允许的 loopback 目标。"""
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False


__all__ = [
    "SSRF_BOUNDARY_NOTE",
    "configure_ssrf_whitelist",
    "validate_url_target",
    "validate_resolved_url",
    "contains_internal_url",
    "_is_private",
    "_normalize_addr",
]
