from __future__ import annotations

import asyncio
from typing import Optional

from nexus.logging import get_logger
from nexus.channels.base import NotificationChannel, VALID_CHANNELS
from nexus.channels.webhook import WebhookChannel
from nexus.sse_manager import SSEManager

logger = get_logger("nexus.channels.dispatcher")


class ChannelDispatcher:
    def __init__(self, sse_manager: Optional[SSEManager] = None) -> None:
        self._channels: dict[str, NotificationChannel] = {}
        self._sse_manager: Optional[SSEManager] = sse_manager
        self._initialized: bool = False

    def register_channel(self, channel: NotificationChannel) -> None:
        self._channels[channel.name] = channel
        logger.info("Channel registered: %s", channel.name)

    def set_sse_manager(self, manager: SSEManager) -> None:
        self._sse_manager = manager

    def init_default_channels(self) -> None:
        if self._initialized:
            return
        self._channels["webhook"] = WebhookChannel()
        logger.info("Webhook channel registered")
        self._initialized = True

    def _filter_channels(self, channels: list[str]) -> list[str]:
        valid: list[str] = []
        for ch in channels:
            if ch not in VALID_CHANNELS:
                logger.warning("Invalid channel filtered out: %s", ch)
                continue
            valid.append(ch)
        return valid

    async def _send_single_channel(
        self,
        channel_name: str,
        notification_dict: dict[str, object],
        user_id: str,
    ) -> Optional[str]:
        if channel_name == "in_app":
            if self._sse_manager is None:
                logger.warning("SSE manager not set, in_app channel unavailable")
                return None
            await self._sse_manager.push(user_id, notification_dict)
            return "in_app"

        channel: Optional[NotificationChannel] = self._channels.get(channel_name)
        if channel is None:
            logger.warning("Channel not registered: %s", channel_name)
            return None

        try:
            ok: bool = await channel.send(notification_dict)
            if ok:
                return channel_name
        except Exception as exc:
            logger.error("Channel %s send error: %s", channel_name, str(exc))
        return None

    async def dispatch(
        self,
        notification_dict: dict[str, object],
        channels: list[str],
        user_id: str,
    ) -> list[str]:
        if not self._initialized:
            self.init_default_channels()

        notif_copy: dict[str, object] = dict(notification_dict)
        notif_copy["user_id"] = user_id
        valid_channels: list[str] = self._filter_channels(channels)

        tasks: list[asyncio.Task[Optional[str]]] = [
            asyncio.create_task(
                self._send_single_channel(ch, notif_copy, user_id)
            )
            for ch in valid_channels
        ]
        results: list[Optional[str]] = await asyncio.gather(*tasks, return_exceptions=False)

        sent: list[str] = [r for r in results if r is not None]
        logger.info(
            "Dispatch completed: channels=%s sent=%s", valid_channels, sent
        )
        return sent

    def get_registered_channels(self) -> list[str]:
        return list(self._channels.keys()) + (["in_app"] if self._sse_manager else [])


__all__ = ["ChannelDispatcher"]
