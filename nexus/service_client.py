"""服务间调用客户端：短效凭证签发与请求转发。"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import httpx

from nexus.defaults import DEFAULT_UC_BASE_URL
from nexus.logging import get_logger

logger = get_logger("nexus.service_client")

_SERVICE_TOKEN_TTL: int = 900
_REFRESH_MARGIN: int = 60


class ServiceClient:
    """统一服务间凭证：UC client_credentials 短效 JWT，缓存 + 自动续期。

    凭证源从活到旧逐级尝试，首个换取成功者胜出：
    1. Lion 当前 namespace business/uc_auth（应用级身份，与 uc_sdk bootstrap 同源，
       UC 应用身份轮换后仍活着的凭证——challengePlanet 10-24 实证）
    2. Lion infra/usercenter app_key/app_secret 或 env UC__APP_KEY/UC__APP_SECRET
       （平台共享凭证，身份轮换后易成死键，仅作回退）
    3. env SERVICE_TOKEN（过渡兜底，最终移除）
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._refreshing_task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    def _uc_base_url() -> str:
        return os.getenv("UC__BASE_URL", "").rstrip("/") or DEFAULT_UC_BASE_URL

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        return self._client

    async def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - _REFRESH_MARGIN:
            return self._token
        if self._refreshing_task is asyncio.current_task():
            return self.get_cached_token()
        async with self._lock:
            if self._token and time.time() < self._expires_at - _REFRESH_MARGIN:
                return self._token
            await self._refresh()
        return self._token

    async def _refresh(self) -> None:
        self._refreshing_task = asyncio.current_task()
        try:
            await self._refresh_locked()
        finally:
            self._refreshing_task = None

    async def _refresh_locked(self) -> None:
        from nexus.infra import get_uc_auth, get_uc_config

        try:
            cfg: dict = await get_uc_config()
        except Exception as exc:
            logger.warning("读取 UC 配置失败: %s", exc)
            cfg = {}
        base_url: str = str(cfg.get("base_url") or "").rstrip("/") or self._uc_base_url()

        sources: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            auth: dict = await get_uc_auth()
        except Exception as exc:
            logger.warning("读取 Lion uc_auth 失败: %s", exc)
            auth = {}
        for name, creds in (
            ("lion:uc_auth", auth),
            ("infra:usercenter", cfg),
        ):
            key = str(creds.get("app_key") or "")
            secret = str(creds.get("app_secret") or "")
            if not key or not secret or (key, secret) in seen:
                continue
            seen.add((key, secret))
            sources.append((name, key, secret))

        for name, key, secret in sources:
            token, expires_in = await self._exchange_token(key, secret, base_url)
            if token:
                self._token = token
                self._expires_at = time.time() + expires_in
                logger.info("service token 续期成功 source=%s ttl=%ss", name, expires_in)
                return
        legacy: str = os.getenv("SERVICE_TOKEN", "")
        if legacy:
            self._token = legacy
            self._expires_at = time.time() + _SERVICE_TOKEN_TTL
            logger.warning("UC 凭证源全部失效，回退 SERVICE_TOKEN 兼容调用")
        else:
            logger.warning("UC 凭证与 SERVICE_TOKEN 均不可用，服务间调用将失败")

    async def _exchange_token(self, app_key: str, app_secret: str, base_url: str) -> tuple[str, int]:
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

    async def delegation_headers(self, user_id: str) -> dict[str, str]:
        """服务委托头：服务 JWT + X-On-Behalf-Of，代表指定用户调用上游用户端点。

        上游 nexus.auth 对服务身份 fail-closed：无委托头的服务凭证在用户端点 403。
        """
        token = await self.get_token()
        if not token or not user_id:
            return {}
        return {
            "Authorization": f"Bearer {token}",
            "X-On-Behalf-Of": str(user_id),
        }

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
