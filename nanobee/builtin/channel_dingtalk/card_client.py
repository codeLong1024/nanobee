"""DingTalk HTTP API client — wraps token management, headers, and error handling.

Token 统一委托给 access_token_fn（DingTalkSender.get_access_token），
不维护独立缓存，消除与 TokenManager 的双重缓存问题。
"""

from __future__ import annotations

from typing import Any

import httpx


class DingTalkCardClient:
    """DingTalk AI Card HTTP client.

    Responsibilities:
    - Async HTTP client lifecycle
    - Header construction with token injection
    - Unified API error handling
    - Token **不做独立缓存**，统一委托给 ``access_token_fn``
    """

    def __init__(
        self,
        api_base: str = "https://api.dingtalk.com",
        access_token_fn: Any = None,
        proxy_url: str | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self._access_token_fn = access_token_fn
        self._proxy_url = proxy_url

        self.async_http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # HTTP client lifecycle
    # ------------------------------------------------------------------

    async def ensure_async_client(self) -> httpx.AsyncClient:
        """Ensure the async HTTP client exists."""
        if self.async_http is None or self.async_http.is_closed:
            kwargs: dict[str, Any] = {"timeout": 30}
            if self._proxy_url:
                kwargs["proxies"] = self._proxy_url
            self.async_http = httpx.AsyncClient(**kwargs)
        return self.async_http

    async def close(self) -> None:
        if self.async_http and not self.async_http.is_closed:
            await self.async_http.aclose()

    # ------------------------------------------------------------------
    # Token management — 不维护独立缓存，统一委托给 access_token_fn
    # ------------------------------------------------------------------

    async def _refresh_token(self) -> str | None:
        """Fetch an access token via the provided callable."""
        if not self._access_token_fn:
            return None
        try:
            return await self._access_token_fn()
        except Exception:
            return None

    async def ensure_valid_token(self) -> str:
        """Return a valid token by delegating to access_token_fn."""
        token = await self._refresh_token()
        if token:
            return token
        raise RuntimeError("Unable to obtain access token")

    async def get_headers_async(self) -> dict[str, str]:
        """Build authorized headers (async, with token refresh)."""
        token = await self.ensure_valid_token()
        return {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token,
        }

    # ------------------------------------------------------------------
    # Unified error handling
    # ------------------------------------------------------------------

    async def check_response(
        self,
        resp: httpx.Response,
        operation: str = "API call",
    ) -> None:
        """Raise HTTPStatusError on non-2xx responses."""
        if resp.status_code == 403 and "QpsLimit" in resp.text:
            raise httpx.HTTPStatusError(
                f"QPS limit: {resp.status_code}", request=resp.request, response=resp,
            )
        elif resp.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"Server error: {resp.status_code}", request=resp.request, response=resp,
            )
        elif resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error: {resp.status_code}", request=resp.request, response=resp,
            )

    # ------------------------------------------------------------------
    # API URL helper
    # ------------------------------------------------------------------

    @property
    def api_url(self) -> str:
        """DingTalk API v1.0 base URL."""
        return f"{self.api_base}/v1.0"


__all__ = ["DingTalkCardClient"]
