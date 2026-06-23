"""Token management for DingTalk API access.

Provides a standalone :class:`TokenManager` that handles OAuth2 token
acquisition, caching, and refresh — usable by both DingTalkSender
and other components.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

import httpx

from nanobee.utils.logger import logger



class TokenManager:
    """DingTalk Access Token manager with caching and auto-refresh.

    Supports two modes:
    1. **Shared provider**: delegating to an external callable (recommended).
    2. **Self-managed**: using client_id + client_secret from config.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient | None = None,
        logger: Any = None,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._logger = logger
        self._token_provider = token_provider

        # Local token cache
        self._access_token: str | None = None
        self._token_expiry: float = 0

    @property
    def http(self) -> httpx.AsyncClient | None:
        return self._http

    @http.setter
    def http(self, client: httpx.AsyncClient | None) -> None:
        self._http = client

    @property
    def token_provider(self) -> Callable[[], Awaitable[str | None]] | None:
        return self._token_provider

    @token_provider.setter
    def token_provider(self, provider: Callable[[], Awaitable[str | None]] | None) -> None:
        self._token_provider = provider

    async def get_access_token(self) -> str | None:
        """Get or refresh Access Token.

        If a shared ``token_provider`` is set, delegates to it.
        Otherwise manages its own OAuth2 flow using ``client_id`` +
        ``client_secret``.
        """
        if self._token_provider is not None:
            return await self._token_provider()

        # Fallback: own token management
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        if not self._http:
            return None

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self._client_id,
            "appSecret": self._client_secret,
        }

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # Expire 60s early to be safe
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception:
            if self._logger:
                self._logger.exception("Failed to get access token")
            return None


__all__ = ["TokenManager"]
