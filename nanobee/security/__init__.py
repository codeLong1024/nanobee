"""Security Policies - 安全策略

提供 SSRF 防护、路径边界校验和网络 URL 安全检测。

子模块：
- network.py: SSRF 前置拦截、CIDR 白名单、内网 URL 检测
- workspace_policy.py: 路径边界解析与校验工具
"""

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

__all__ = [
    # network
    "configure_ssrf_whitelist",
    "validate_url_target",
    "validate_resolved_url",
    "contains_internal_url",
    "_is_private",
    "_normalize_addr",
    "_normalize_hostname",
    # workspace_policy
    "WORKSPACE_BOUNDARY_NOTE",
    "resolve_path",
    "is_path_within",
    "is_path_allowed",
    "require_path_within",
    "resolve_allowed_path",
]
