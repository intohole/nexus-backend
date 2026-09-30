"""Web 请求周边：CORS 源解析、客户端 IP 解析、健康检查注册表。"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from fastapi import Request


def resolve_cors_origins(value: object) -> list[str]:
    if isinstance(value, str):
        return [o.strip() for o in value.split(",") if o.strip()]
    if isinstance(value, (list, tuple)):
        return [str(o).strip() for o in value if str(o).strip()]
    return []


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], Awaitable[bool]]] = {}

    def register(self, name: str, check_func: Callable[[], Awaitable[bool]]) -> None:
        self._checks[name] = check_func

    async def _run_check(
        self, name: str, check_func: Callable[[], Awaitable[bool]]
    ) -> tuple[str, bool]:
        try:
            result: bool = await check_func()
            return name, result
        except Exception:
            return name, False

    async def run_all(self) -> dict[str, bool]:
        if not self._checks:
            return {}
        tasks: list[Awaitable[tuple[str, bool]]] = [
            self._run_check(name, func)
            for name, func in self._checks.items()
        ]
        resultsList: list[tuple[str, bool]] = await asyncio.gather(*tasks)
        return dict(resultsList)

    async def is_healthy(self) -> bool:
        results: dict[str, bool] = await self.run_all()
        return all(results.values())
