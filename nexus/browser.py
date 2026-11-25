"""browserHub 客户端:渲染页证据原语(snapshot/证据矩阵/正文提取/PDF),供各应用与 agent 复用。

r247 收归轮新增:URL→截图/正文/PDF,产物一律返回万象 rel 引用(禁止 base64);
storage_state 仅请求级透传(browserhub 服务端零落盘,无历史读取端点)。
消费方一行式:rel = (await snapshot(url))["asset_rel"]。
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from nexus.discovery import get_registry, resolve_cached
from nexus.logging import get_logger
from nexus.service_client import get_service_token
from nexus.trace import outbound_headers

logger = get_logger("nexus.browser")


class BrowserHubError(RuntimeError):
    """browserHub 调用失败。调用方决定降级策略,本原语不静默吞错。

    r249:429 busy 携带结构化退避面(status_code/retry_after_ms/queue_depth),
    调用方可程序化退避;其余错误三字段为 None。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after_ms: Optional[int] = None,
        queue_depth: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms
        self.queue_depth = queue_depth


def get_browserhub_base_url() -> str:
    env_url = os.environ.get("BROWSERHUB_BASE_URL", "").rstrip("/")
    if env_url:
        return env_url
    cached = resolve_cached("browserhub")
    if cached:
        return cached
    return get_registry().bootstrap("browserhub")


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:200] or resp.reason_phrase
    return str(body.get("detail") or body)[:200]


class BrowserHubClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = (base_url or get_browserhub_base_url()).rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def _post(self, path: str, payload: dict[str, Any]) -> dict:
        token = await get_service_token()
        headers = outbound_headers({"X-Service-Token": token} if token else None)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                resp = await client.post(f"{self._base_url}{path}", json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise BrowserHubError(f"browserhub 不可达: {exc}") from exc
        if resp.status_code != 200:
            retry_after: Optional[int] = None
            queue_depth: Optional[int] = None
            if resp.status_code == 429:
                try:
                    busy = resp.json().get("detail") or {}
                    retry_after = busy.get("retry_after_ms")
                    queue_depth = busy.get("queue_depth")
                except Exception:
                    pass
            raise BrowserHubError(
                f"browserhub {resp.status_code}: {_detail(resp)}",
                status_code=resp.status_code,
                retry_after_ms=retry_after,
                queue_depth=queue_depth,
            )
        return resp.json()

    async def snapshot(
        self,
        url: str,
        *,
        viewport: Optional[dict[str, int]] = None,
        theme: str = "light",
        full_page: bool = False,
        wait_until: str = "networkidle",
        storage_state: Optional[dict] = None,
        asset_app: Optional[str] = None,
        include_aria: bool = False,
        block_media: Optional[bool] = None,
        allow_private: Optional[bool] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "url": url,
            "theme": theme,
            "full_page": full_page,
            "wait_until": wait_until,
            "include_aria": include_aria,
        }
        if viewport:
            payload["viewport"] = viewport
        if storage_state is not None:
            payload["storage_state"] = storage_state
        if asset_app:
            payload["asset_app"] = asset_app
        if block_media is not None:
            payload["block_media"] = block_media
        if allow_private is not None:
            payload["allow_private"] = allow_private
        return await self._post("/api/browser/v1/snapshot", payload)

    async def aria(
        self,
        url: str,
        *,
        viewport: Optional[dict[str, int]] = None,
        theme: str = "light",
        wait_until: str = "networkidle",
        storage_state: Optional[dict] = None,
        aria_mode: str = "ai",
        aria_depth: Optional[int] = None,
        block_media: Optional[bool] = None,
        allow_private: Optional[bool] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "url": url,
            "theme": theme,
            "wait_until": wait_until,
            "aria_mode": aria_mode,
        }
        if viewport:
            payload["viewport"] = viewport
        if storage_state is not None:
            payload["storage_state"] = storage_state
        if aria_depth is not None:
            payload["aria_depth"] = aria_depth
        if block_media is not None:
            payload["block_media"] = block_media
        if allow_private is not None:
            payload["allow_private"] = allow_private
        return await self._post("/api/browser/v1/aria", payload)

    async def matrix(
        self,
        url: str,
        *,
        viewports: Optional[list[dict[str, int]]] = None,
        themes: Optional[list[str]] = None,
        full_page: bool = False,
        wait_until: str = "networkidle",
        block_media: Optional[bool] = None,
        allow_private: Optional[bool] = None,
    ) -> dict:
        payload: dict[str, Any] = {"url": url, "full_page": full_page, "wait_until": wait_until}
        if viewports:
            payload["viewports"] = viewports
        if themes:
            payload["themes"] = themes
        if block_media is not None:
            payload["block_media"] = block_media
        if allow_private is not None:
            payload["allow_private"] = allow_private
        return await self._post("/api/browser/v1/matrix", payload)

    async def extract(
        self,
        url: str,
        *,
        viewport: Optional[dict[str, int]] = None,
        wait_until: str = "networkidle",
        fmt: str = "text",
        storage_state: Optional[dict] = None,
        wait_selector: Optional[str] = None,
        block_media: Optional[bool] = None,
        allow_private: Optional[bool] = None,
    ) -> dict:
        payload: dict[str, Any] = {"url": url, "wait_until": wait_until, "format": fmt}
        if viewport:
            payload["viewport"] = viewport
        if storage_state is not None:
            payload["storage_state"] = storage_state
        if wait_selector:
            payload["wait_selector"] = wait_selector
        if block_media is not None:
            payload["block_media"] = block_media
        if allow_private is not None:
            payload["allow_private"] = allow_private
        return await self._post("/api/browser/v1/extract", payload)

    async def pdf(
        self, url: str, *, allow_private: Optional[bool] = None
    ) -> dict:
        payload: dict[str, Any] = {"url": url}
        if allow_private is not None:
            payload["allow_private"] = allow_private
        return await self._post("/api/browser/v1/pdf", payload)


_client: Optional[BrowserHubClient] = None


def get_browser_client() -> BrowserHubClient:
    global _client
    if _client is None:
        _client = BrowserHubClient()
    return _client


async def snapshot(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().snapshot(url, **kwargs)


async def aria(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().aria(url, **kwargs)


async def matrix(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().matrix(url, **kwargs)


async def extract(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().extract(url, **kwargs)


async def pdf(url: str) -> dict:
    return await get_browser_client().pdf(url)
