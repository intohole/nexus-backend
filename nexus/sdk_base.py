"""SDK HTTP 脚手架基类：client 生命周期 + 统一错误信封，bm/chroma/lion/uc 共用。

子类差异点通过两个钩子表达：
- _headers(): 每请求附加的鉴权头（X-Service-Token / Authorization / X-API-Key）；
- _envelope(): 响应 → dict 的归一（默认 raise_for_status + json；Lion 覆写为 code 信封）。
请求失败统一返回 {"success": False, "detail": ...}，成功返回响应 dict。
"""
from __future__ import annotations

import json
from typing import Optional

import httpx


class BaseAsyncClient:
    service_name: str = "remote"

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        client_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self._timeout: float = timeout
        self._client_headers: dict[str, str] = client_headers or {}
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers=self._client_headers or None,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "BaseAsyncClient":
        await self._get_client()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()

    async def _headers(self) -> dict[str, str]:
        return {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Optional[dict[str, object]] = None,
        data: Optional[dict[str, object]] = None,
        params: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        client = await self._get_client()
        headers: dict[str, str] = await self._headers()
        try:
            response = await client.request(
                method, path, headers=headers, json=json_data, data=data, params=params,
            )
            return self._envelope(response)
        except httpx.HTTPStatusError as e:
            return self._status_error(e)
        except httpx.ConnectError:
            return {"success": False, "detail": f"Cannot connect to {self.service_name} at {self.base_url}"}
        except httpx.TimeoutException:
            return {"success": False, "detail": f"{self.service_name} request timeout at {self.base_url}"}
        except httpx.RequestError as e:
            return {"success": False, "detail": f"{self.service_name} request error: {e}"}
        except (json.JSONDecodeError, ValueError):
            return {"success": False, "detail": f"{self.service_name} response parse error"}

    def _envelope(self, response: httpx.Response) -> dict[str, object]:
        """默认归一：非 2xx 抛 HTTPStatusError，成功返回响应 JSON。"""
        response.raise_for_status()
        return response.json()

    def _status_error(self, exc: httpx.HTTPStatusError) -> dict[str, object]:
        detail = exc.response.text
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                detail = body.get("detail", body.get("message", detail))
        except (json.JSONDecodeError, ValueError):
            pass
        return {"success": False, "detail": f"HTTP {exc.response.status_code}: {detail}"}

    @staticmethod
    def _is_error(result: dict[str, object]) -> bool:
        return isinstance(result, dict) and result.get("success") is False
