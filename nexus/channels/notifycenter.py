"""notifyCenter 直发渠道：把通知送达统一通知中心，支持深链与 per-app 静音。"""
from __future__ import annotations

from loguru import logger

from nexus.channels.base import NotificationChannel


class NotifyCenterChannel(NotificationChannel):
    """notification dict 必须带 user_id（通知中心按用户投递）；
    app_id/link/type/priority 可选，priority 为 1-3 的通知中心级别；
    channels 可选列表透传通知中心（email/wecom 等），缺省 in_app。
    占位 user（default/空/unknown/0/guest/anon_* 前缀）由 send_receipt
    单源拦截——这类投递永远无人可见，只会在通知中心积累死行（goldenFish
    曾向 default 连发 6 条停摆通知）。
    link 契约：商业端应用（app_group=商业端，公网入口在 biz 域）必须传绝对 URL，
    相对链接在主域的铃铛/通知面板渲染时不可达、点击即 404（truemirror r2 修复
    后 goldenFish r19 复踩同型——消费侧无法安全改写相对链接，发送侧负责绝对化）。"""

    def __init__(self) -> None:
        super().__init__("notifycenter")

    async def send(self, notification: dict[str, object]) -> bool:
        from nexus.notify_api import send_receipt

        raw_channels = notification.get("channels")
        channels = (
            [str(c).strip() for c in raw_channels if str(c).strip()]
            if isinstance(raw_channels, list) else None
        )
        notify_id, result = await send_receipt(
            user_id=str(notification.get("user_id") or "").strip(),
            title=str(notification.get("title", "")),
            content=str(notification.get("content", "")),
            type=str(notification.get("notify_type", "") or "system"),
            priority=int(notification.get("priority", 1) or 1),
            app_id=str(notification.get("app_id", "") or "system"),
            link=str(notification.get("link", "")),
            channels=channels,
            dedup_key=str(notification.get("dedup_key") or ""),
        )
        if notify_id <= 0:
            logger.warning(
                "NotifyCenter send failed for user=%s result=%s",
                notification.get("user_id"), result,
            )
            return False
        return True


__all__ = ["NotifyCenterChannel"]
