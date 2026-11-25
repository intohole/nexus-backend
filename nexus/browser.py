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
    """browserHub 调用失败。调用方决定降级策略,本原语不静默吞错。"""


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
        timeout: float = 90.0,
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
            raise BrowserHubError(f"browserhub {resp.status_code}: {_detail(resp)}")
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
    ) -> dict:
        payload: dict[str, Any] = {
            "url": url,
            "theme": theme,
            "full_page": full_page,
            "wait_until": wait_until,
        }
        if viewport:
            payload["viewport"] = viewport
        if storage_state is not None:
            payload["storage_state"] = storage_state
        if asset_app:
            payload["asset_app"] = asset_app
        return await self._post("/api/browser/v1/snapshot", payload)

    async def matrix(
        self,
        url: str,
        *,
        viewports: Optional[list[dict[str, int]]] = None,
        themes: Optional[list[str]] = None,
        full_page: bool = False,
        wait_until: str = "networkidle",
    ) -> dict:
        payload: dict[str, Any] = {"url": url, "full_page": full_page, "wait_until": wait_until}
        if viewports:
            payload["viewports"] = viewports
        if themes:
            payload["themes"] = themes
        return await self._post("/api/browser/v1/matrix", payload)

    async def extract(
        self, url: str, *, viewport: Optional[dict[str, int]] = None, wait_until: str = "networkidle"
    ) -> dict:
        payload: dict[str, Any] = {"url": url, "wait_until": wait_until}
        if viewport:
            payload["viewport"] = viewport
        return await self._post("/api/browser/v1/extract", payload)

    async def pdf(self, url: str) -> dict:
        return await self._post("/api/browser/v1/pdf", {"url": url})


_client: Optional[BrowserHubClient] = None


def get_browser_client() -> BrowserHubClient:
    global _client
    if _client is None:
        _client = BrowserHubClient()
    return _client


async def snapshot(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().snapshot(url, **kwargs)


async def matrix(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().matrix(url, **kwargs)


async def extract(url: str, **kwargs: Any) -> dict:
    return await get_browser_client().extract(url, **kwargs)


async def pdf(url: str) -> dict:
    return await get_browser_client().pdf(url)
