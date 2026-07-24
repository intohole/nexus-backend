from __future__ import annotations

import os
from typing import Optional

import httpx

from nexus.logging import get_logger
from nexus.utils import HttpClient

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
            or os.environ.get("NOTIFY_CENTER_URL", "http://localhost:8910")
        )
        self._service_token: str = service_token or os.environ.get(
            "SERVICE_TOKEN", ""
        )
        self._timeout: float = timeout
        self._http: HttpClient = HttpClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-Service-Token": self._service_token},
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def send(
        self,
        user_id: str,
        title: str,
        content: str = "",
        type: str = "system",
        priority: int = 1,
        app_id: str = "system",
        data: Optional[dict[str, object]] = None,
        link: str = "",
        channels: Optional[list[str]] = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
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
        try:
            resp: httpx.Response = await self._http.post(
                "/api/notify/send",
                json=payload,
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
        await self._http.close()


_notify_client: Optional[NotifyClient] = None


def get_notify_client() -> NotifyClient:
    global _notify_client
    if _notify_client is None:
        _notify_client = NotifyClient()
    return _notify_client


async def send_notification(
    user_id: str,
    title: str,
    content: str = "",
    **kwargs: object,
) -> dict[str, object]:
    client: NotifyClient = get_notify_client()
    return await client.send(
        user_id=user_id, title=title, content=content, **kwargs
    )


__all__ = [
    "NotifyClient",
    "get_notify_client",
    "send_notification",
]
