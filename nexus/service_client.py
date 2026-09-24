"""服务间调用客户端：短效凭证签发与请求转发。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import httpx

from nexus.defaults import DEFAULT_UC_BASE_URL
from nexus.logging import get_logger

logger = get_logger("nexus.service_client")

_SERVICE_TOKEN_TTL: int = 900
_REFRESH_MARGIN: int = 60
_UC_CRED_CACHE_TTL: int = 3600


class ServiceClient:
    """统一服务间凭证：UC client_credentials 短效 JWT，缓存 + 自动续期。

    凭证来源优先级：
    1. env UC_APP_KEY/UC_APP_SECRET（miniDeploy 部署时注入，最终态）
    2. Lion 当前 namespace business/uc_auth（迁移期引导，用 SERVICE_TOKEN 读一次后缓存）
    3. env SERVICE_TOKEN（过渡兜底，最终移除）
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self._uc_cred: tuple[str, str] = ("", "")
        self._uc_cred_fetched_at: float = 0.0

    @staticmethod
    def _uc_base_url() -> str:
        return os.getenv("UC_BASE_URL", "").rstrip("/") or DEFAULT_UC_BASE_URL

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        return self._client

    async def _load_uc_credentials(self) -> tuple[str, str]:
        app_key: str = os.getenv("UC_APP_KEY", "")
        app_secret: str = os.getenv("UC_APP_SECRET", "")
        if app_key and app_secret:
            return app_key, app_secret
        if self._uc_cred[0] and time.time() - self._uc_cred_fetched_at < _UC_CRED_CACHE_TTL:
            return self._uc_cred
        key, secret = await self._fetch_uc_auth_from_lion()
        if key and secret:
            self._uc_cred = (key, secret)
            self._uc_cred_fetched_at = time.time()
            return key, secret
        return app_key, app_secret

    async def _fetch_uc_auth_from_lion(self) -> tuple[str, str]:
        lion_base: str = os.getenv("LION_BASE_URL", "").rstrip("/")
        namespace: str = os.getenv("LION_NAMESPACE", "")
        if not lion_base or not namespace:
            return "", ""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{lion_base}/api/v1/namespaces/{namespace}/configs/business/uc_auth",
                headers={"X-Service-Token": os.getenv("SERVICE_TOKEN", "")},
            )
            if resp.status_code != 200:
                return "", ""
            data: dict = resp.json()
            value = (data.get("data") or {}).get("value", "")
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                return "", ""
            return str(value.get("app_key") or ""), str(value.get("app_secret") or "")
        except Exception as exc:
            logger.warning("从 Lion 读取 UC 凭证失败: %s", exc)
            return "", ""

    async def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - _REFRESH_MARGIN:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expires_at - _REFRESH_MARGIN:
                return self._token
            await self._refresh()
        return self._token

    async def _refresh(self) -> None:
        app_key, app_secret = await self._load_uc_credentials()
        if app_key and app_secret:
            token, expires_in = await self._exchange_token(app_key, app_secret)
            if token:
                self._token = token
                self._expires_at = time.time() + expires_in
                logger.info("service token 续期成功 ttl=%ss", expires_in)
                return
        legacy: str = os.getenv("SERVICE_TOKEN", "")
        if legacy:
            self._token = legacy
            self._expires_at = time.time() + _SERVICE_TOKEN_TTL
            logger.warning("UC 凭证换取失败或不可用，回退 SERVICE_TOKEN 兼容调用")
        else:
            logger.warning("UC 凭证与 SERVICE_TOKEN 均不可用，服务间调用将失败")

    async def _exchange_token(self, app_key: str, app_secret: str) -> tuple[str, int]:
        base_url: str = self._uc_base_url()
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{base_url}/api/auth/token",
                json={
                    "grant_type": "client_credentials",
                    "app_key": app_key,
                    "app_secret": app_secret,
                },
            )
            if resp.status_code != 200:
                logger.warning("service token 获取失败: status=%s", resp.status_code)
                return "", 0
            data: dict = resp.json()
            payload: dict = data.get("data") if isinstance(data, dict) else {}
            token: str = str(payload.get("access_token") or "")
            expires_in: int = int(payload.get("expires_in") or _SERVICE_TOKEN_TTL)
            if not token:
                logger.warning("service token 响应缺少 access_token")
                return "", 0
            return token, expires_in
        except Exception as exc:
            logger.warning("service token 获取异常: %s", exc)
            return "", 0

    async def header(self) -> dict[str, str]:
        token = await self.get_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def get_cached_token(self) -> str:
        if self._token and time.time() < self._expires_at - _REFRESH_MARGIN:
            return self._token
        return os.environ.get("SERVICE_TOKEN", "")

    async def close(self) -> None:
        self._token = ""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


_client: Optional[ServiceClient] = None


def get_service_client() -> ServiceClient:
    global _client
    if _client is None:
        _client = ServiceClient()
    return _client


async def get_service_token() -> str:
    return await get_service_client().get_token()
