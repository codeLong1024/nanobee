"""DingTalk 媒体读取安全策略测试（Phase 1 安全前置）。

锁定四件事：
1. SSRF 判定在仓内只有一份实现——媒体读取委托 ``nanobee.security.network``，
   本地不再自持字符串黑名单（同一事实只有一个真相源）；
2. 绕过写法（十进制/八进制/十六进制 IP、链接本地元数据段、IPv6 私网与映射）
   全部被拒，重定向逐跳复检；
3. 本地附件读取受白名单根约束，``..`` 穿越、符号链接逃逸、``file://`` 越界均拒绝；
4. 体积上限（远端 + 本地）与 ``enable_media_upload`` 总开关生效。

本地策略注入点为 :meth:`DingTalkSender.set_media_policy`（由通道 ``start()`` 调用）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobee.builtin.channel_dingtalk.config import DingTalkConfig
from nanobee.builtin.channel_dingtalk.media import fetch as fetch_mod
from nanobee.builtin.channel_dingtalk.media.fetch import (
    fetch_remote_media_bytes,
    read_media_bytes,
)
from nanobee.builtin.channel_dingtalk.sender import DingTalkSender
from nanobee.kernel import NanobeeKernel
from nanobee.kernel.core_parser import CoreMDParser
from nanobee.plugins.base import PluginMetadata
from nanobee.security import network as network_mod
from nanobee.security.network import configure_ssrf_whitelist, validate_url_target

# ============================================================
# Fakes — 只实现 fetch 用到的 stream() 接口，避免真实网络
# ============================================================


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int = 200,
        url: str = "http://93.184.216.34/media.bin",
        headers: dict[str, str] | None = None,
        body: bytes = b"payload",
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeStreamCtx:
    def __init__(self, resp: _FakeStreamResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._resp

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeHttp:
    """按调用顺序吐出预置响应，并记录实际请求的 URL（验证未跟进被拒跳转）。"""

    def __init__(self, responses: list[_FakeStreamResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def stream(self, method: str, url: str, follow_redirects: bool = False) -> _FakeStreamCtx:
        self.calls.append(url)
        return _FakeStreamCtx(self._responses.pop(0))


# 无需 DNS 即可判定的被拒写法（字面 IP / 非标准 IP 字面量）
BLOCKED_REMOTE_URLS = [
    "http://169.254.169.254/latest/meta-data/",   # 云元数据段
    "http://2130706433/",                          # 十进制整数 → 127.0.0.1
    "http://0xa9fea9fe/",                          # 十六进制 → 169.254.169.254
    "http://0251.0376.0251.0376/",                 # 八进制 → 169.254.169.254
    "http://[::ffff:127.0.0.1]/",                  # IPv6 映射 loopback
    "http://[fd00::1]/",                           # IPv6 ULA
    "http://[fe80::1]/",                           # IPv6 link-local
    "http://127.0.0.1/",
    "http://10.1.2.3/",
    "http://192.168.1.1/",
]

PUBLIC_REMOTE_URL = "http://93.184.216.34/report.md"
NON_HTTP_URL = "ftp://93.184.216.34/x"


# ============================================================
# 委托：仓内只有一份 SSRF 判定实现
# ============================================================


class TestSharedValidatorDelegation:
    def test_validators_are_the_shared_implementation(self) -> None:
        """fetch 模块导出的校验函数必须就是 security.network 的那两个对象。"""
        assert fetch_mod.validate_url_target is network_mod.validate_url_target
        assert fetch_mod.validate_resolved_url is network_mod.validate_resolved_url

    def test_remote_validation_delegates(self) -> None:
        """远端读取必须调用共享实现，而不是模块内的本地定义。"""
        assert fetch_mod.validate_url_target.__module__ == "nanobee.security.network"

    @pytest.mark.asyncio
    async def test_remote_read_uses_the_shared_validator(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        def _recording(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
            calls.append(url)
            return False, "blocked-by-test"

        monkeypatch.setattr(fetch_mod, "validate_url_target", _recording)
        data, _, _ = await read_media_bytes(_FakeHttp([]), PUBLIC_REMOTE_URL)

        assert data is None
        assert calls == [PUBLIC_REMOTE_URL]


# ============================================================
# 远端：绕过表 / 重定向 / 体积上限
# ============================================================


class TestRemoteMediaSecurity:
    @pytest.mark.parametrize("url", BLOCKED_REMOTE_URLS)
    def test_blocked_urls_rejected_by_shared_validator(self, url: str) -> None:
        ok, _ = validate_url_target(url)
        assert not ok

    @pytest.mark.parametrize("url", BLOCKED_REMOTE_URLS)
    @pytest.mark.asyncio
    async def test_blocked_urls_not_fetched(self, url: str) -> None:
        http = _FakeHttp([])
        data, filename, content_type = await read_media_bytes(http, url)

        assert (data, filename, content_type) == (None, None, None)
        assert http.calls == []  # 校验在发请求前完成

    @pytest.mark.asyncio
    async def test_public_url_is_downloaded(self) -> None:
        http = _FakeHttp([_FakeStreamResponse(body=b"hello")])
        data, filename, content_type = await read_media_bytes(http, PUBLIC_REMOTE_URL)

        assert data == b"hello"
        assert filename == "report.md"
        assert content_type is None

    @pytest.mark.asyncio
    async def test_non_http_scheme_refused_by_remote_fetch(self) -> None:
        """远端层拒绝非 http(s) scheme，且不发请求。"""
        http = _FakeHttp([])
        data, _ = await fetch_remote_media_bytes(http, NON_HTTP_URL)

        assert data is None
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_non_http_scheme_not_treated_as_remote(self) -> None:
        """非 http(s) 引用按本地路径处理——白名单外仍拒绝，且不发请求。"""
        http = _FakeHttp([])
        data, _, _ = await read_media_bytes(http, NON_HTTP_URL)

        assert data is None
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_remote_over_limit_refused(self) -> None:
        http = _FakeHttp([_FakeStreamResponse(body=b"x" * 64)])
        data, _, _ = await read_media_bytes(
            http, PUBLIC_REMOTE_URL, max_bytes=16,
        )

        assert data is None

    @pytest.mark.asyncio
    async def test_cross_host_redirect_refused(self) -> None:
        """未列入白名单的跨主机跳转直接拒绝，且不发出第二跳请求。"""
        http = _FakeHttp([
            _FakeStreamResponse(
                status_code=302,
                url="http://93.184.216.34/a",
                headers={"location": "http://1.1.1.1/a"},
            ),
        ])
        data, _ = await fetch_remote_media_bytes(
            http, "http://93.184.216.34/a", allow_remote_media_redirects=True,
        )

        assert data is None
        assert len(http.calls) == 1

    @pytest.mark.asyncio
    async def test_redirect_to_private_address_refused(self) -> None:
        """主机在允许名单内、但目标是私网/元数据地址 → 逐跳复检拒绝。"""
        http = _FakeHttp([
            _FakeStreamResponse(
                status_code=302,
                url="http://93.184.216.34/a",
                headers={"location": "http://169.254.169.254/latest/meta-data/"},
            ),
        ])
        data, _ = await fetch_remote_media_bytes(
            http,
            "http://93.184.216.34/a",
            allow_remote_media_redirects=True,
            remote_media_redirect_allowed_hosts={"169.254.169.254"},
        )

        assert data is None
        assert len(http.calls) == 1


# ============================================================
# 本地：白名单根 / 逃逸 / 体积上限 / 总开关
# ============================================================


class TestLocalMediaWhitelist:
    @pytest.mark.asyncio
    async def test_file_inside_root_is_read(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        report = root / "report.md"
        report.write_bytes(b"# report")

        data, filename, _ = await read_media_bytes(None, str(report), local_roots=[str(root)])

        assert data == b"# report"
        assert filename == "report.md"

    @pytest.mark.asyncio
    async def test_symlink_inside_root_is_read(self, tmp_path: Path) -> None:
        """根内符号链接指向根内文件 → 正常放行（不过度拦截）。"""
        root = tmp_path / "data"
        root.mkdir()
        target = root / "real.md"
        target.write_bytes(b"ok")
        link = root / "link.md"
        link.symlink_to(target)

        data, _, _ = await read_media_bytes(None, str(link), local_roots=[str(root)])

        assert data == b"ok"

    @pytest.mark.asyncio
    async def test_file_outside_root_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"top-secret")

        data, _, _ = await read_media_bytes(None, str(secret), local_roots=[str(root)])

        assert data is None

    @pytest.mark.asyncio
    async def test_dotdot_traversal_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        (tmp_path / "secret.txt").write_bytes(b"top-secret")

        data, _, _ = await read_media_bytes(
            None, str(root / ".." / "secret.txt"), local_roots=[str(root)],
        )

        assert data is None

    @pytest.mark.asyncio
    async def test_symlink_escape_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"top-secret")
        link = root / "innocent.md"
        link.symlink_to(secret)

        data, _, _ = await read_media_bytes(None, str(link), local_roots=[str(root)])

        assert data is None

    @pytest.mark.asyncio
    async def test_file_url_inside_root_allowed(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        report = root / "report.md"
        report.write_bytes(b"# report")

        data, _, _ = await read_media_bytes(None, f"file://{report}", local_roots=[str(root)])

        assert data == b"# report"

    @pytest.mark.asyncio
    async def test_file_url_outside_root_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"top-secret")

        data, _, _ = await read_media_bytes(None, f"file://{secret}", local_roots=[str(root)])

        assert data is None

    @pytest.mark.asyncio
    async def test_missing_roots_denies_local_read(self, tmp_path: Path) -> None:
        """未注入白名单根 ⇒ 默认拒绝（不做"未配置即全放行"的静默兜底）。"""
        report = tmp_path / "report.md"
        report.write_bytes(b"data")

        data, _, _ = await read_media_bytes(None, str(report))

        assert data is None

    @pytest.mark.asyncio
    async def test_directory_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()

        data, _, _ = await read_media_bytes(None, str(root), local_roots=[str(root)])

        assert data is None

    @pytest.mark.asyncio
    async def test_local_over_limit_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        big = root / "big.bin"
        big.write_bytes(b"x" * 64)

        data, _, _ = await read_media_bytes(
            None, str(big), local_roots=[str(root)], max_bytes=16,
        )

        assert data is None

    @pytest.mark.asyncio
    async def test_media_switch_off_refuses_local(self, tmp_path: Path) -> None:
        """enable_media_upload=False ⇒ 本地读取拒绝（总开关）。"""
        root = tmp_path / "data"
        root.mkdir()
        report = root / "report.md"
        report.write_bytes(b"# report")

        data, _, _ = await read_media_bytes(
            None, str(report), local_roots=[str(root)], allow_media=False,
        )

        assert data is None

    @pytest.mark.asyncio
    async def test_media_switch_off_refuses_remote(self) -> None:
        """enable_media_upload=False ⇒ 远端读取同样拒绝，且不发请求。"""
        http = _FakeHttp([])

        data, _, _ = await read_media_bytes(http, PUBLIC_REMOTE_URL, allow_media=False)

        assert data is None
        assert http.calls == []


# ============================================================
# 策略注入：DingTalkSender.set_media_policy（配置真正生效）
# ============================================================


class TestSenderMediaPolicy:
    @staticmethod
    def _sender() -> DingTalkSender:
        return DingTalkSender(DingTalkConfig(), MagicMock())

    @pytest.mark.asyncio
    async def test_default_policy_denies_local_read(self, tmp_path: Path) -> None:
        """未注入策略时拒绝本地读取（fail-closed）。"""
        report = tmp_path / "report.md"
        report.write_bytes(b"data")

        data, _, _ = await self._sender().read_media_bytes(str(report))

        assert data is None

    @pytest.mark.asyncio
    async def test_injected_roots_enable_local_read(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        report = root / "report.md"
        report.write_bytes(b"# report")

        sender = self._sender()
        sender.set_media_policy(max_bytes=1024, local_roots=[str(root)], allow_media=True)

        data, filename, _ = await sender.read_media_bytes(str(report))

        assert data == b"# report"
        assert filename == "report.md"

    @pytest.mark.asyncio
    async def test_injected_max_bytes_is_enforced(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        root.mkdir()
        big = root / "big.bin"
        big.write_bytes(b"x" * 64)

        sender = self._sender()
        sender.set_media_policy(max_bytes=16, local_roots=[str(root)])

        data, _, _ = await sender.read_media_bytes(str(big))

        assert data is None

    @pytest.mark.asyncio
    async def test_upload_switch_off_denies_media(self, tmp_path: Path) -> None:
        """策略注入的总开关关闭时，本地与远端一律拒绝。"""
        root = tmp_path / "data"
        root.mkdir()
        report = root / "report.md"
        report.write_bytes(b"# report")

        sender = self._sender()
        sender.set_media_policy(local_roots=[str(root)], allow_media=False)

        data, _, _ = await sender.read_media_bytes(str(report))
        remote, _, _ = await sender.read_media_bytes(PUBLIC_REMOTE_URL)

        assert data is None
        assert remote is None


# ============================================================
# 白名单根解析：data_dir + media_local_roots
# ============================================================


class TestLocalRootResolution:
    @staticmethod
    def _plugin(data_dir: Any) -> Any:
        from nanobee.builtin.channel_dingtalk.channel import DingTalkChannelPlugin

        plugin = DingTalkChannelPlugin.__new__(DingTalkChannelPlugin)
        plugin.__init__(metadata=PluginMetadata(name="channel_dingtalk", plugin_type="channel"))
        plugin.logger = MagicMock()
        plugin._kernel = SimpleNamespace(data_dir=data_dir)
        return plugin

    def test_data_dir_plus_relative_and_absolute_extra_roots(self, tmp_path: Path) -> None:
        plugin = self._plugin(tmp_path)
        extra_rel = tmp_path / "extra"
        extra_rel.mkdir()
        extra_abs = tmp_path / "abs_extra"

        roots = plugin._resolve_media_local_roots(
            DingTalkConfig(media_local_roots=["extra", str(extra_abs)]),
        )

        assert roots[0] == str(tmp_path.resolve())
        assert str(extra_rel.resolve()) in roots
        assert str(extra_abs.resolve()) in roots

    def test_extra_roots_deduplicated_with_data_dir(self, tmp_path: Path) -> None:
        plugin = self._plugin(tmp_path)

        roots = plugin._resolve_media_local_roots(
            DingTalkConfig(media_local_roots=[str(tmp_path)]),
        )

        assert roots.count(str(tmp_path.resolve())) == 1

    def test_missing_data_dir_keeps_only_extra_roots(self, tmp_path: Path) -> None:
        plugin = self._plugin(None)
        extra = tmp_path / "extra"
        extra.mkdir()

        roots = plugin._resolve_media_local_roots(DingTalkConfig(media_local_roots=[str(extra)]))

        assert str(extra.resolve()) in roots

    def test_blank_extra_root_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """空串 / "." 不参与解析——否则会把当前工作目录整棵放行。"""
        monkeypatch.chdir(tmp_path)
        plugin = self._plugin(tmp_path)

        roots = plugin._resolve_media_local_roots(DingTalkConfig(media_local_roots=["", ".", "  "]))

        assert roots == [str(tmp_path.resolve()), str((tmp_path / "media" / "dingtalk").resolve())]

    def test_inbound_download_root_always_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """入站附件目录（./media/dingtalk）必须放行——否则用户自己的附件回投会被拒。"""
        monkeypatch.chdir(tmp_path)
        plugin = self._plugin(tmp_path / "data")

        roots = plugin._resolve_media_local_roots(DingTalkConfig())

        assert str((tmp_path / "media" / "dingtalk").resolve()) in roots

    @pytest.mark.asyncio
    async def test_inbound_attachment_readable_under_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """白名单根覆盖入站目录时，入站附件可读（回投路径不回归）。"""
        monkeypatch.chdir(tmp_path)
        plugin = self._plugin(tmp_path / "data")
        inbound = tmp_path / "media" / "dingtalk" / "u1" / "report.md"
        inbound.parent.mkdir(parents=True)
        inbound.write_bytes(b"# inbound")

        roots = plugin._resolve_media_local_roots(DingTalkConfig())
        data, _, _ = await read_media_bytes(None, str(inbound), local_roots=roots)

        assert data == b"# inbound"


# ============================================================
# tools.ssrf_whitelist 接线（Kernel 初始化）
# ============================================================


class TestSsrfWhitelistWiring:
    def _make_kernel(self, tmp_path: Path, whitelist: list[str]) -> NanobeeKernel:
        CoreMDParser.create_default(tmp_path / "core.md")
        return NanobeeKernel(config={
            "data_dir": str(tmp_path),
            "core_md_path": str(tmp_path / "core.md"),
            "tools": {"ssrf_whitelist": whitelist},
        })

    def test_kernel_passes_config_whitelist_to_security(self, tmp_path: Path) -> None:
        try:
            self._make_kernel(tmp_path, ["10.0.0.0/8"])
            # 白名单生效：该 CIDR 不再被判为私有；未列出的私网仍拒绝
            assert validate_url_target("http://10.1.2.3/")[0] is True
            assert validate_url_target("http://192.168.1.1/")[0] is False
        finally:
            configure_ssrf_whitelist([])

    def test_empty_whitelist_keeps_default_deny(self, tmp_path: Path) -> None:
        """空白名单复位全局状态——多实例/测试交替不残留。"""
        try:
            self._make_kernel(tmp_path, [])
            assert validate_url_target("http://10.1.2.3/")[0] is False
        finally:
            configure_ssrf_whitelist([])
