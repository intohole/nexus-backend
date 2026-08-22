from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from nexus.logging import get_logger

logger = get_logger("nexus.chat.events")

Listener = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChatEventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    def subscribe(self, event: str, listener: Listener) -> None:
        self._listeners.setdefault(event, []).append(listener)

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        for listener in self._listeners.get(event, []):
            try:
                await listener(event, payload)
            except Exception as exc:
                logger.warning("chat event listener failed [event=%s]: %s", event, exc)
