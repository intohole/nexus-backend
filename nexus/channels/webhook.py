from __future__ import annotations

import httpx

from nexus.logging import get_logger
from nexus.channels.base import NotificationChannel

logger = get_logger("nexus.channels.webhook")


class WebhookChannel(NotificationChannel):
    def __init__(self, timeout: float = 10.0) -> None:
        super().__init__("webhook")
        self._timeout: float = timeout

    async def send(self, notification: dict[str, object]) -> bool:
        webhook_url: str = str(notification.get("webhook_url", ""))
        if not webhook_url:
            logger.debug("No webhook URL in notification data, skipping")
            return False

        payload: dict[str, object] = {
            "title": notification.get("title", ""),
            "content": notification.get("content", ""),
            "type": notification.get("type", "system"),
            "priority": notification.get("priority", 1),
            "data": notification.get("data", {}),
            "timestamp": notification.get("created_at", ""),
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp: httpx.Response = await client.post(webhook_url, json=payload)
                if resp.status_code < 300:
                    logger.info("Webhook sent to %s: %s", webhook_url, resp.status_code)
                    return True
                logger.warning(
                    "Webhook failed: url=%s status=%s", webhook_url, resp.status_code
                )
                return False
        except Exception as exc:
            logger.error("Webhook send error: %s", str(exc))
            return False


__all__ = ["WebhookChannel"]
