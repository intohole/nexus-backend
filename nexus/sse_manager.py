from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from nexus.logging import get_logger

logger = get_logger("nexus.sse")

MAX_CONNECTIONS_PER_USER: int = 5


class SSEConnectionError(Exception):
    pass


class SSEManager:
    def __init__(self, max_connections_per_user: int = MAX_CONNECTIONS_PER_USER) -> None:
        self._connections: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._max_per_user: int = max_connections_per_user

    async def connect(self, user_id: str) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=100)
        async with self._lock:
            current: int = len(self._connections.get(user_id, set()))
            if current >= self._max_per_user:
                raise SSEConnectionError(
                    f"Max connections reached for user {user_id}: {self._max_per_user}"
                )
            if user_id not in self._connections:
                self._connections[user_id] = set()
            self._connections[user_id].add(queue)
        logger.info(
            "SSE connected: user=%s, total=%d", user_id, self.get_user_count(user_id)
        )
        return queue

    async def disconnect(
        self, user_id: str, queue: asyncio.Queue[dict[str, object]]
    ) -> None:
        async with self._lock:
            if user_id in self._connections:
                self._connections[user_id].discard(queue)
                if not self._connections[user_id]:
                    del self._connections[user_id]
        logger.info("SSE disconnected: user=%s", user_id)

    async def push(self, user_id: str, data: dict[str, object]) -> int:
        pushed: int = 0
        async with self._lock:
            queues: set[asyncio.Queue[dict[str, object]]] = self._connections.get(
                user_id, set()
            )
            for q in queues:
                try:
                    q.put_nowait(data)
                    pushed += 1
                except asyncio.QueueFull:
                    logger.warning(
                        "SSE queue full for user=%s, dropping", user_id
                    )
        return pushed

    async def disconnect_all(self) -> None:
        async with self._lock:
            total: int = self.get_total_connections()
            self._connections.clear()
        logger.info("All SSE connections cleared: %d total", total)

    def get_user_count(self, user_id: str) -> int:
        return len(self._connections.get(user_id, set()))

    def get_total_connections(self) -> int:
        return sum(len(queues) for queues in self._connections.values())

    def get_connections_detail(self) -> dict[str, int]:
        return {
            uid: len(queues) for uid, queues in self._connections.items()
        }


async def sse_event_generator(
    manager: SSEManager,
    user_id: str,
    heartbeat_interval: float = 15.0,
    event_name: str = "notification",
) -> AsyncGenerator[str, None]:
    try:
        queue: asyncio.Queue[dict[str, object]] = await manager.connect(user_id)
    except SSEConnectionError as exc:
        logger.warning("SSE connection rejected: %s", str(exc))
        return

    try:
        yield ": connected\n\n"
        while True:
            try:
                data: dict[str, object] = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_interval
                )
                payload: str = json.dumps(data, ensure_ascii=False, default=str)
                yield f"event: {event_name}\ndata: {payload}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        await manager.disconnect(user_id, queue)


__all__ = [
    "SSEManager",
    "SSEConnectionError",
    "sse_event_generator",
    "MAX_CONNECTIONS_PER_USER",
]
