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


def test_sliding_window_current_count() -> None:
    window: SlidingWindow = SlidingWindow(3, 60)
    assert window.current_count() == 0
    asyncio.run(window.is_allowed())
    asyncio.run(window.is_allowed())
    assert window.current_count() == 2
    assert window.is_exceeded() is False


def _rule_app() -> FastAPI:
    from nexus.rate_limit import PathRule

    app: FastAPI = FastAPI()

    @app.get("/api/llm/chat")
    async def llm_chat() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/api/other")
    async def other() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=10,
        requests_per_hour=0,
        exclude_paths=[],
        route_rules=[PathRule("/api/llm", 1)],
    )
    return app


def test_route_rules_tier_prefix_limit() -> None:
    client: TestClient = TestClient(_rule_app())
    assert client.get("/api/llm/chat").status_code == 200
    assert client.get("/api/llm/chat").status_code == 429
    assert client.get("/api/other").status_code == 200
    assert client.get("/api/other").status_code == 200


def test_route_rules_longest_prefix_wins() -> None:
    from nexus.rate_limit import PathRule

    app: FastAPI = FastAPI()

    @app.get("/api/llm/chat")
    async def llm_chat() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/api/other")
    async def other() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=10,
        requests_per_hour=0,
        exclude_paths=[],
        route_rules=[PathRule("/api", 5), PathRule("/api/llm", 1)],
    )
    client: TestClient = TestClient(app)
    assert client.get("/api/llm/chat").status_code == 200
    assert client.get("/api/llm/chat").status_code == 429
    for _ in range(5):
        assert client.get("/api/other").status_code == 200
    assert client.get("/api/other").status_code == 429


def test_limit_response_hook_custom_body() -> None:
    from starlette.responses import PlainTextResponse

    from nexus.rate_limit import LimitInfo

    def hook(request: Request, info: LimitInfo) -> PlainTextResponse:
        return PlainTextResponse(
            f"blocked:{info.scope}:{info.retry_after}",
            status_code=429,
            headers={"Retry-After": str(info.retry_after)},
        )

    app: FastAPI = FastAPI()

    @app.get("/x")
    async def x() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=1,
        requests_per_hour=0,
        exclude_paths=[],
        limit_response=hook,
    )
    client: TestClient = TestClient(app)
    assert client.get("/x").status_code == 200
    resp = client.get("/x")
    assert resp.status_code == 429
    assert resp.text.startswith("blocked:minute:")
    assert int(resp.headers["Retry-After"]) >= 1


def test_default_429_carries_remaining_header() -> None:
    app: FastAPI = FastAPI()

    @app.get("/y")
    async def y() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=2,
        requests_per_hour=0,
        exclude_paths=[],
    )
    client: TestClient = TestClient(app)
    assert client.get("/y").status_code == 200
    assert client.get("/y").status_code == 200
    resp = client.get("/y")
    assert resp.status_code == 429
    assert resp.headers["X-RateLimit-Limit"] == "2"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    body: dict = resp.json()
    assert body["error_code"] == "RATE_LIMIT_EXCEEDED"


def test_zero_global_rpm_disables_global_window() -> None:
    app: FastAPI = FastAPI()

    @app.get("/free")
    async def free() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=0,
        requests_per_hour=0,
        exclude_paths=[],
    )
    client: TestClient = TestClient(app)
    for _ in range(30):
        assert client.get("/free").status_code == 200


def test_rule_only_mode_keeps_rule_limit() -> None:
    from nexus.rate_limit import PathRule

    app: FastAPI = FastAPI()

    @app.get("/api/x")
    async def x() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/free")
    async def free() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(
        RateLimitMiddleware,
        config=NexusConfig(),
        requests_per_minute=0,
        requests_per_hour=0,
        exclude_paths=[],
        route_rules=[PathRule("/api/", 1)],
    )
    client: TestClient = TestClient(app)
    assert client.get("/api/x").status_code == 200
    assert client.get("/api/x").status_code == 429
    for _ in range(5):
        assert client.get("/free").status_code == 200
