from __future__ import annotations

from nexus.channels.base import NotificationChannel
from nexus.logging import get_logger
from nexus.notify import get_notify_client

logger = get_logger("nexus.channels.email")


class EmailChannel(NotificationChannel):
    def __init__(self) -> None:
        super().__init__("email")

    async def send(self, notification: dict[str, object]) -> bool:
        data: dict[str, object] = notification.get("data", {})
        email: str = str(
            notification.get("email") or data.get("email") or data.get("to") or ""
        )
        if not email:
            logger.debug("No email address in notification data, skipping")
            return False
        subject: str = str(notification.get("title", "通知"))
        body: str = str(notification.get("content", ""))
        try:
            ok: bool = await get_notify_client().send_email(to=email, subject=subject, body=body)
            logger.info("Email sent to %s: %s", email, ok)
            return ok
        except Exception as exc:
            logger.error("Email send error: %s", str(exc))
            return False


__all__ = ["EmailChannel"]