"""notifyCenter 直发渠道：把通知送达统一通知中心，支持深链与 per-app 静音。"""
from __future__ import annotations

from loguru import logger

from nexus.channels.base import NotificationChannel


class NotifyCenterChannel(NotificationChannel):
    """notification dict 必须带 user_id（通知中心按用户投递）；
    app_id/link/type/priority 可选，priority 为 1-3 的通知中心级别。"""

    def __init__(self) -> None:
        super().__init__("notifycenter")

    async def send(self, notification: dict[str, object]) -> bool:
        user_id = str(notification.get("user_id") or "")
        if not user_id:
            logger.warning("NotifyCenter notification missing user_id, skipped")
            return False
        from nexus.notify import get_notify_client

        result = await get_notify_client().send(
            user_id=user_id,
            title=str(notification.get("title", ""))[:200],
            content=str(notification.get("content", ""))[:2000],
            type=str(notification.get("notify_type", "") or "system"),
            priority=int(notification.get("priority", 1) or 1),
            app_id=str(notification.get("app_id", "") or "system"),
            link=str(notification.get("link", "")),
        )
        if not result:
            logger.warning("NotifyCenter send failed for user=%s", user_id)
            return False
        return True


__all__ = ["NotifyCenterChannel"]
