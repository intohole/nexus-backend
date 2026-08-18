import os
import asyncio
import time
import logging
import httpx
from typing import Dict, Any, List

try:
    from jose import jwt, JWTError
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

from .pkce import PKCEHelper
from .auth import AuthMixin
from .mixins import (
    UserMixin, AppMixin, VipMixin, InviteCodeMixin,
    ThirdPartyMixin, DiscoveryMixin, ApiTokenMixin, SessionMixin, AuditMixin,
)

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 60.0):
        self._failure_count: int = 0
        self._failure_threshold: int = failure_threshold
        self._reset_timeout: float = reset_timeout
        self._open_until: float = 0

    @property
    def is_open(self) -> bool:
        if self._failure_count < self._failure_threshold:
            return False
        if time.time() >= self._open_until:
            self._failure_count = 0
            return False
        return True

    def record_failure(self):
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._open_until = time.time() + self._reset_timeout
            logger.warning(f"断路器打开: failure_count={self._failure_count}, reset_in={self._reset_timeout}s")

    def record_success(self):
        self._failure_count = 0


class BlacklistCache:
    def __init__(self, ttl: float = 30.0, max_size: int = 10000):
        self._cache: Dict[str, tuple] = {}
        self._ttl: float = ttl
        self._max_size: int = max_size
        self._last_sync_at: float = 0
        self._sync_interval: float = 60.0
        self._lock = None

    def _get_lock(self):
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        return self._lock

    async def is_blacklisted(self, jti: str) -> bool | None:
        async with self._get_lock():
            if jti not in self._cache:
                return None
            is_blacklisted, cached_at = self._cache[jti]
            if time.time() - cached_at > self._ttl:
                del self._cache[jti]
                return None
            return is_blacklisted

    async def mark_blacklisted(self, jti: str):
        async with self._get_lock():
            self._cache[jti] = (True, time.time())
            if len(self._cache) > self._max_size:
                self._evict_expired()

    async def mark_valid(self, jti: str):
        async with self._get_lock():
            self._cache[jti] = (False, time.time())

    def needs_sync(self) -> bool:
        return time.time() - self._last_sync_at > self._sync_interval

    def mark_synced(self):
        self._last_sync_at = time.time()

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, (_, t) in self._cache.items() if now - t > self._ttl]
        for k in expired:
            del self._cache[k]


class UserCenterSDK(AuthMixin, UserMixin, AppMixin, VipMixin, InviteCodeMixin,
                    ThirdPartyMixin, DiscoveryMixin, ApiTokenMixin, SessionMixin, AuditMixin):
    def __init__(self, base_url: str = "", app_key: str = None, app_secret: str = None,
                 client_id: str = None, jwt_secret_key: str = None, timeout: float = 10.0):
        if not base_url:
            base_url = os.environ.get("UC_BASE_URL", "http://localhost:8901")
        self.base_url = base_url.rstrip("/")
        self.app_key = app_key or client_id
        self.client_id = self.app_key
        self._app_secret = app_secret
        self.app_secret = app_secret
        self.jwt_secret_key = jwt_secret_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float | None = None
        self._service_token: str | None = None
        self._service_token_expires_at: float = 0
        self._service_token_refresh_margin: int = 300
        self._bg_task: asyncio.Task | None = None
        self._refresh_interval: int = 300
        self._bootstrap_retry_delay: int = 30
        self._blacklist_cache = BlacklistCache(ttl=30.0)
        self._circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout=60.0)
        self._max_retries: int = 2
        self._pkce_state: Dict[str, str] = {}
        self._jwks_keys: List[Dict[str, Any]] = []
        self._jwks_by_kid: Dict[str, Dict[str, Any]] = {}
        self._jwks_fetched_at: float = 0
        self._jwks_refresh_interval: int = 300

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"Content-Type": "application/json"}
            )
        return self._client

    async def close(self):
        if self._bg_task is not None:
            self._bg_task.cancel()
            self._bg_task = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    def is_authenticated(self) -> bool:
        return self._access_token is not None

    def is_configured(self) -> bool:
        return bool(self.app_key and self.base_url)

    def _set_tokens(self, data: Dict[str, Any]):
        self._access_token = data.get("access_token")
        if data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
        if data.get("expires_in"):
            self._token_expires_at = time.time() + data["expires_in"]

    def clear_tokens(self):
        self._access_token = None
        self._refresh_token = None
        self._token_expires_at = None

    async def _request(self, method: str, path: str, token: str | None = None,
                       skip_refresh: bool = False, **kwargs) -> Dict[str, Any]:
        if self._circuit_breaker.is_open:
            return {"success": False, "detail": "认证服务暂时不可用，请稍后重试"}

        client = await self._get_client()
        headers = kwargs.pop("headers", {})
        use_token = token or self._access_token
        if not use_token and self._service_token:
            use_token = await self._ensure_service_token()
        if use_token:
            headers["Authorization"] = f"Bearer {use_token}"

        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.request(method, path, headers=headers, **kwargs)
                break
            except httpx.ConnectError as e:
                last_error = e
                logger.warning(f"UC连接失败(attempt={attempt + 1}): {e}")
                if attempt < self._max_retries:
                    await self._sleep(0.5 * (attempt + 1))
                    continue
                self._circuit_breaker.record_failure()
                return {"success": False, "detail": "认证服务连接失败，请稍后重试"}
            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"UC请求超时(attempt={attempt + 1}): {e}")
                if attempt < self._max_retries:
                    await self._sleep(0.5 * (attempt + 1))
                    continue
                self._circuit_breaker.record_failure()
                return {"success": False, "detail": "认证服务响应超时，请稍后重试"}
            except httpx.RequestError as e:
                last_error = e
                logger.error(f"UC请求异常: {e}")
                self._circuit_breaker.record_failure()
                return {"success": False, "detail": "认证服务请求异常，请稍后重试"}

        if response.status_code == 401 and self._refresh_token and not skip_refresh and not token:
            refreshed = await self.refresh_access_token()
            if refreshed:
                headers["Authorization"] = f"Bearer {self._access_token}"
                try:
                    response = await client.request(method, path, headers=headers, **kwargs)
                except httpx.RequestError as e:
                    logger.error(f"UC刷新后请求异常: {e}")
                    return {"success": False, "detail": "认证服务请求异常，请稍后重试"}

        if response.status_code >= 500:
            logger.error(f"UC服务端错误: status={response.status_code}")
            self._circuit_breaker.record_failure()
            return {"success": False, "detail": "认证服务内部错误，请稍后重试"}

        self._circuit_breaker.record_success()

        if response.status_code >= 400:
            try:
                error_data = response.json()
                detail = error_data.get("detail", error_data.get("message", f"请求失败({response.status_code})"))
            except Exception:
                detail = f"请求失败({response.status_code})"
            return {"success": False, "detail": detail}

        return response.json()

    @staticmethod
    async def _sleep(seconds: float):
        import asyncio
        await asyncio.sleep(seconds)

    async def bootstrap(self) -> bool:
        if not self._app_secret:
            logger.info("无app_secret，跳过service token引导")
            return False
        try:
            result = await self.client_credentials()
            if result.get("success"):
                self._service_token = self._access_token
                self._service_token_expires_at = self._token_expires_at or (time.time() + 1800)
                self._access_token = None
                self._refresh_token = None
                self._token_expires_at = None
                self.app_secret = None
                logger.info(f"Service token引导成功，app_secret已清除，有效期至{time.strftime('%H:%M:%S', time.localtime(self._service_token_expires_at))}")
                try:
                    await self._refresh_jwks()
                except Exception as exc:
                    logger.warning(f"引导后JWKS预取失败: {exc}")
                return True
            else:
                logger.warning(f"Service token引导失败: {result.get('detail')}")
                return False
        except Exception as e:
            logger.warning(f"Service token引导异常: {e}")
            return False

    async def _ensure_service_token(self) -> str | None:
        if not self._service_token or time.time() > self._service_token_expires_at - self._service_token_refresh_margin:
            if self._app_secret:
                saved_secret = self._app_secret
                self.app_secret = saved_secret
                refreshed = await self.bootstrap()
                if not refreshed:
                    self.app_secret = None
        return self._service_token

    async def _refresh_jwks_if_stale(self) -> None:
        if time.time() - self._jwks_fetched_at < self._jwks_refresh_interval:
            return
        await self._refresh_jwks()

    async def _refresh_jwks(self) -> None:
        try:
            client = await self._get_client()
            response = await client.get("/.well-known/jwks.json")
            if response.status_code >= 400:
                logger.warning(f"JWKS拉取失败: status={response.status_code}")
                return
            data = response.json()
            keys = data.get("keys", [])
            self._jwks_keys = keys
            self._jwks_by_kid = {k.get("kid"): k for k in keys if k.get("kid")}
            self._jwks_fetched_at = time.time()
            logger.info(f"UC JWKS已缓存: keys={len(keys)}")
        except Exception as exc:
            logger.warning(f"JWKS拉取异常: {exc}")

    async def start_background_refresh(self) -> None:
        if self._bg_task is not None and not self._bg_task.done():
            return
        self._bg_task = asyncio.create_task(self._bg_loop())
        logger.info("UC background refresh started")

    async def _bg_loop(self) -> None:
        delay: float = self._refresh_interval
        while True:
            await asyncio.sleep(delay)
            try:
                await self._refresh_jwks_if_stale()
            except Exception as exc:
                logger.warning("UC JWKS refresh error: %s", exc)
            if not self._app_secret:
                continue
            try:
                if self._service_token:
                    await self._ensure_service_token()
                    delay = self._refresh_interval
                else:
                    await self.bootstrap()
                    delay = self._bootstrap_retry_delay if not self._service_token else self._refresh_interval
            except Exception as exc:
                logger.warning("UC service token refresh error: %s", exc)
                delay = self._bootstrap_retry_delay

    async def stop_background_refresh(self) -> None:
        if self._bg_task is not None:
            self._bg_task.cancel()
            self._bg_task = None