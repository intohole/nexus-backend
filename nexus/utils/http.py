"""共享 httpx.AsyncClient 生命周期封装：懒初始化 + 显式关闭 + 便捷动词。"""
from __future__ import annotations

import asyncio
from typing import Optional, cast

import httpx


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: Optional[object] = None,
        headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = False,
    ) -> None:
        self._base_url: str = base_url
        self._timeout: object = timeout if timeout is not None else 30.0
        self._headers: dict[str, str] = headers or {}
        self._follow_redirects: bool = follow_redirects
        self._client: Optional[httpx.AsyncClient] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def init(self) -> None:
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=cast(httpx.Timeout | float, self._timeout),
                headers=self._headers,
                follow_redirects=self._follow_redirects,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        if self._client is None:
            await self.init()
        if self._client is None:
            raise RuntimeError("HttpClient initialization failed.")
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    @classmethod
    async def stream_all(
        cls,
        method: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, object]] = None,
        json: Optional[object] = None,
        timeout: Optional[httpx.Timeout] = None,
        follow_redirects: bool = False,
    ):
        async with httpx.AsyncClient(
            timeout=timeout or httpx.Timeout(300.0),
            follow_redirects=follow_redirects,
        ) as client:
            async with client.stream(
                method, url, headers=headers, params=params, json=json
            ) as resp:
                yield resp
