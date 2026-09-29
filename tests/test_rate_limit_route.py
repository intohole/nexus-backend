from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nexus.config import NexusConfig
from nexus.rate_limit import RateLimitMiddleware, SlidingWindow, parse_rate_limit, rate_limit


def test_sliding_window_readonly_exceeded() -> None:
    window: SlidingWindow = SlidingWindow(2, 60)
    assert window.is_exceeded() is False
    assert asyncio.run(window.is_allowed()) is True
    assert window.is_exceeded() is False
    assert asyncio.run(window.is_allowed()) is True
    assert window.is_exceeded() is True
    assert asyncio.run(window.is_allowed()) is False
    assert window.is_exceeded() is True


def _app(scope: str) -> FastAPI:
    app: FastAPI = FastAPI()

    @app.get("/limited")
    @rate_limit("2/minute", scope=scope)
    async def limited(request: Request) -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/plain")
    async def plain(request: Request) -> dict[str, str]:
        return {"ok": "1"}

    return app


def test_parse_rate_limit_units() -> None:
    assert parse_rate_limit("20/minute") == (20, 60)
    assert parse_rate_limit("5/second") == (5, 1)
    assert parse_rate_limit("3/hours") == (3, 3600)
    assert parse_rate_limit("7/day") == (7, 86400)
    for bad in ("abc", "20/fortnight", "20"):
        try:
            parse_rate_limit(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_route_limit_blocks_after_budget_with_retry_after() -> None:
    client: TestClient = TestClient(_app("test-budget"))
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    resp = client.get("/limited")
    assert resp.status_code == 429
    assert resp.json()["detail"] == "请求过于频繁，请稍后重试"
    assert int(resp.headers["Retry-After"]) >= 1


def test_route_limit_isolates_keys() -> None:
    def key_func(request: Request) -> str:
        return request.headers.get("x-key", "") or "anonymous"

    app: FastAPI = FastAPI()

    @app.get("/keyed")
    @rate_limit("1/minute", key_func=key_func)
    async def keyed(request: Request) -> dict[str, str]:
        return {"ok": "1"}

    client: TestClient = TestClient(app)
    assert client.get("/keyed", headers={"x-key": "a"}).status_code == 200
    assert client.get("/keyed", headers={"x-key": "a"}).status_code == 429
    assert client.get("/keyed", headers={"x-key": "b"}).status_code == 200


def test_disabled_limit_passes_through() -> None:
    app: FastAPI = FastAPI()

    @app.get("/off")
    @rate_limit(lambda: "")
    async def off(request: Request) -> dict[str, str]:
        return {"ok": "1"}

    client: TestClient = TestClient(app)
    for _ in range(5):
        assert client.get("/off").status_code == 200


def test_global_middleware_skips_route_with_own_limit() -> None:
    app: FastAPI = _app("test-middleware-skip")
    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=1,
        requests_per_hour=0,
        exclude_paths=[],
    )
    client: TestClient = TestClient(app)
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    assert client.get("/plain").status_code == 200
    assert client.get("/plain").status_code == 429
