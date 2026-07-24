from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from nexus.logging import get_logger

logger = get_logger("nexus.notify")


class NotifyClient:
    def __init__(
        self,
        base_url: str = "",
        service_token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.environ.get("NOTIFY_CENTER_URL", "http://localhost:8902")
        )
        self._service_token: str = service_token or os.environ.get(
            "SERVICE_TOKEN", ""
        )
        self._timeout: float = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def send(
        self,
        user_id: str,
        title: str,
        content: str = "",
        type: str = "system",
        priority: int = 1,
        app_id: str = "system",
        data: Optional[dict[str, Any]] = None,
        link: str = "",
        channels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_id": user_id,
            "app_id": app_id,
            "type": type,
            "priority": priority,
            "title": title,
            "content": content,
            "data": data or {},
            "link": link,
            "channels": channels or ["in_app"],
        }
        headers: dict[str, str] = {"X-Service-Token": self._service_token}
        try:
            client: httpx.AsyncClient = await self._get_client()
            resp: httpx.Response = await client.post(
                f"{self._base_url}/api/notify/send",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Notify send failed: status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return {}
        except Exception as exc:
            logger.error("Notify send error: %s", str(exc))
            return {}

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


_notify_client: Optional[NotifyClient] = None


def get_notify_client() -> NotifyClient:
    global _notify_client
    if _notify_client is None:
        _notify_client = NotifyClient()
    return _notify_client


def configure_notify_client(
    base_url: str = "",
    service_token: str = "",
) -> NotifyClient:
    global _notify_client
    _notify_client = NotifyClient(base_url=base_url, service_token=service_token)
    return _notify_client


async def send_notification(
    user_id: str,
    title: str,
    content: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    client: NotifyClient = get_notify_client()
    return await client.send(
        user_id=user_id, title=title, content=content, **kwargs
    )
