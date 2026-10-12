"""通用 GET 响应缓存中间件：TTL + Stale-While-Revalidate + single-flight，仅用于纯只读接口。

key = 方法 + 路径 + 排序后的 query + Authorization 摘要（用户态隔离）。
仅缓存 200 响应；不含写失效，接入方须保证规则内接口为纯只读。
过期后宽限期内返回旧值（X-Cache: STALE）并后台重建，重建走内层 ASGI 栈（不重复经过外层中间件）。
纯 ASGI 接口（scope/receive/send），经 app.add_middleware 注册。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from nexus.logging import get_logger

logger = get_logger("nexus.response_cache")

_MAX_BODY_BYTES = 512 * 1024
_EXCLUDED_HEADERS = {"content-length", "date", "server", "transfer-encoding", "connection", "x-cache"}

Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class CacheRule:
    prefix: str
    ttl: float
    swr_grace: float = 600.0
    methods: tuple[str, ...] = ("GET",)


class ResponseCacheMiddleware:
    def __init__(self, app, rules: list[CacheRule], maxsize: int = 2048) -> None:
        self.app = app
        self._rules = sorted(rules, key=lambda r: len(r.prefix), reverse=True)
        self._maxsize = maxsize
        self._store: dict[str, tuple[float, bytes, int, list[tuple[str, str]]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def __call__(self, scope: dict, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        rule = self._match(request)
        if rule is None:
            await self.app(scope, receive, send)
            return
        key = self._cache_key(request)
        entry = self._store.get(key)
        if entry is not None:
            age = time.monotonic() - entry[0]
            if age < rule.ttl:
                await self._send_stored(send, entry, "HIT")
                return
            if age < rule.ttl + rule.swr_grace:
                self._kick_rebuild(request, key)
                await self._send_stored(send, entry, "STALE")
                return
        inflight = self._tasks.get(key)
        if inflight is not None:
            try:
                await asyncio.shield(inflight)
            except Exception:
                pass
            entry = self._store.get(key)
            if entry is not None:
                await self._send_stored(send, entry, "REVALIDATED")
                return
        await self._rebuild(scope, receive, send, key)

    def _match(self, request: Request) -> CacheRule | None:
        for rule in self._rules:
            if request.method in rule.methods and request.url.path.startswith(rule.prefix):
                return rule
        return None

    def _cache_key(self, request: Request) -> str:
        auth = request.headers.get("authorization", "")
        who = hashlib.sha1(auth.encode("utf-8")).hexdigest()[:12] if auth else "-"
        query = "&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))
        return f"{request.method} {request.url.path}?{query} u={who}"

    async def _send_stored(self, send: Send, entry: tuple[float, bytes, int, list[tuple[str, str]]], hit: str) -> None:
        _, body, status, headers = entry
        out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        out.append((b"x-cache", hit.encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": body})

    async def _rebuild(self, scope: dict, receive: Receive, send: Send, key: str) -> None:
        messages: list[dict] = []

        async def _capture(message: dict) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"x-cache", b"MISS")]
            messages.append(message)
            await send(message)

        await self.app(scope, receive, _capture)
        self._store_from_messages(key, messages)

    def _kick_rebuild(self, request: Request, key: str) -> None:
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(self._background_rebuild(request, key))

    async def _background_rebuild(self, request: Request, key: str) -> None:
        try:
            scope = dict(request.scope)

            async def _receive() -> dict:
                return {"type": "http.disconnect"}

            messages: list[dict] = []

            async def _send(message: dict) -> None:
                messages.append(message)

            await self.app(scope, _receive, _send)
            self._store_from_messages(key, messages)
        except Exception as exc:
            logger.debug(f"响应缓存后台重建失败: {exc}")
        finally:
            self._tasks.pop(key, None)

    def _store_from_messages(self, key: str, messages: list[dict]) -> None:
        start = next((m for m in messages if m.get("type") == "http.response.start"), None)
        if start is None or start.get("status") != 200:
            return
        body = b"".join(m.get("body", b"") for m in messages if m.get("type") == "http.response.body")
        if len(body) > _MAX_BODY_BYTES:
            return
        headers = [
            (k.decode("latin-1"), v.decode("latin-1"))
            for k, v in start.get("headers", [])
            if k.decode("latin-1").lower() not in _EXCLUDED_HEADERS
        ]
        self._store[key] = (time.monotonic(), body, start["status"], headers)
        if len(self._store) > self._maxsize:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            self._store.pop(oldest, None)
