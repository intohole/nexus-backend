"""群机器人通知渠道：企业微信/钉钉/Telegram/Bark webhook 投递。"""
from __future__ import annotations

from nexus.logging import get_logger
from nexus.channels.base import NotificationChannel
from nexus.notify import send_webhook_robot

logger = get_logger("nexus.channels.robot")


def _channel_params(notification: dict[str, object]) -> dict[str, str]:
    data: object = notification.get("data", {}) or {}
    if not isinstance(data, dict):
        data = {}
    return {
        "webhook_url": str(
            notification.get("webhook_url") or data.get("webhook_url") or ""
        ),
        "api_key": str(notification.get("api_key") or data.get("api_key") or ""),
        "chat_id": str(notification.get("chat_id") or data.get("chat_id") or ""),
    }


class WeChatRobotChannel(NotificationChannel):
    def __init__(self) -> None:
        super().__init__("wechat")

    async def send(self, notification: dict[str, object]) -> bool:
        params: dict[str, str] = _channel_params(notification)
        ok: bool = await send_webhook_robot(
            "wechat",
            params["webhook_url"],
            str(notification.get("title", "")),
            str(notification.get("content", "")),
            str(notification.get("level", "info")),
        )
        if not ok:
            logger.warning("WeChat robot notification send failed")
        return ok


class DingTalkChannel(NotificationChannel):
    def __init__(self) -> None:
        super().__init__("dingtalk")

    async def send(self, notification: dict[str, object]) -> bool:
        params: dict[str, str] = _channel_params(notification)
        ok: bool = await send_webhook_robot(
            "dingtalk",
            params["webhook_url"],
            str(notification.get("title", "")),
            str(notification.get("content", "")),
            str(notification.get("level", "info")),
        )
        if not ok:
            logger.warning("DingTalk notification send failed")
        return ok


class TelegramChannel(NotificationChannel):
    def __init__(self) -> None:
        super().__init__("telegram")

    async def send(self, notification: dict[str, object]) -> bool:
        params: dict[str, str] = _channel_params(notification)
        ok: bool = await send_webhook_robot(
            "telegram",
            params["webhook_url"],
            str(notification.get("title", "")),
            str(notification.get("content", "")),
            str(notification.get("level", "info")),
            api_key=params["api_key"],
            chat_id=params["chat_id"],
        )
        if not ok:
            logger.warning("Telegram notification send failed")
        return ok


class BarkChannel(NotificationChannel):
    def __init__(self) -> None:
        super().__init__("bark")

    async def send(self, notification: dict[str, object]) -> bool:
        params: dict[str, str] = _channel_params(notification)
        ok: bool = await send_webhook_robot(
            "bark",
            params["webhook_url"],
            str(notification.get("title", "")),
            str(notification.get("content", "")),
            str(notification.get("level", "info")),
            api_key=params["api_key"],
        )
        if not ok:
            logger.warning("Bark notification send failed")
        return ok


__all__ = [
    "WeChatRobotChannel",
    "DingTalkChannel",
    "TelegramChannel",
    "BarkChannel",
]
