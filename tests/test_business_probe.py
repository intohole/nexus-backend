from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import nexus.business_probe as bp
from nexus import build_probe_runner, register_probe_endpoints


class _FakeScheduler:
    def __init__(self, jobs: dict[str, object], last_tick: str | None = "2026-10-12T06:00:00") -> None:
        self._jobs: dict[str, object] = jobs
        self._last_tick: str | None = last_tick

    def status(self) -> dict[str, object]:
        return {"alive": True, "last_tick_at": self._last_tick, "jobs": self._jobs}

    def ensure_alive(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    bp._cache.clear()


def _app(**kwargs: object) -> TestClient:
    app = FastAPI()
    register_probe_endpoints(app, app_name="demo", **kwargs)  # type: ignore[arg-type]
    return TestClient(app)


def _get(client: TestClient, fresh: bool = False) -> dict:
    headers = {"X-Service-Token": "t"}
    os.environ["SERVICE_TOKEN"] = "t"
    try:
        resp = client.get(f"/api/_internal/probe{'?fresh=1' if fresh else ''}", headers=headers)
    finally:
        os.environ.pop("SERVICE_TOKEN", None)
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture()
def sqlite_factory() -> async_sessionmaker:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    engine.sync_engine.dispose()


def test_probe_requires_service_token() -> None:
    app = FastAPI()
    register_probe_endpoints(app, app_name="demo", llm=False, notify=False)
    assert TestClient(app).get("/api/_internal/probe").status_code == 401


def test_probe_all_skipped_overall_ok() -> None:
    data = _get(_app(llm=False, notify=False))
    assert data["schema"] == 1
    assert data["app"] == "demo"
    assert data["overall"] == "ok"
    assert data["cached"] is False
    for name in ("llm", "db", "notify", "scheduler"):
        assert data["checks"][name]["status"] == "skipped"
        assert data["checks"][name]["evidence"]["reason"] == f"no_{name}_consumer"


def test_probe_llm_custom_check() -> None:
    async def llm() -> dict:
        return {"status": "ok", "latency_ms": 5, "detail": "d", "evidence": {}}

    data = _get(_app(llm=llm, notify=False))
    assert data["checks"]["llm"]["status"] == "ok"


def test_probe_overall_fail_on_any_fail() -> None:
    async def llm() -> dict:
        return {"status": "fail", "latency_ms": None, "detail": "down", "evidence": {}}

    data = _get(_app(llm=llm, notify=False))
    assert data["overall"] == "fail"
    assert data["checks"]["llm"]["detail"] == "down"


def test_probe_scheduler_standard_check() -> None:
    recent: str = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    good = _FakeScheduler({"job-a": {"next_run_at": "2026-10-12T07:00:00"}}, last_tick=recent)
    data = _get(_app(llm=False, notify=False, scheduler=good))
    assert data["checks"]["scheduler"]["status"] == "ok"

    never = _FakeScheduler({"job-a": {"next_run_at": None}}, last_tick=None)
    bp._cache.clear()
    data = _get(_app(llm=False, notify=False, scheduler=never))
    assert data["checks"]["scheduler"]["status"] == "fail"
    assert "job-a" in data["checks"]["scheduler"]["evidence"]["never_scheduled"]


def test_probe_scheduler_custom_check_beats_object() -> None:
    async def sched() -> dict:
        return {"status": "degraded", "latency_ms": 1, "detail": "lagging", "evidence": {}}

    data = _get(_app(llm=False, notify=False, scheduler=_FakeScheduler({}), scheduler_check=sched))
    assert data["checks"]["scheduler"]["status"] == "degraded"
    assert data["overall"] == "degraded"


def test_probe_db_standard_check(sqlite_factory: async_sessionmaker) -> None:
    data = _get(_app(llm=False, notify=False, db_session_factory=sqlite_factory))
    db = data["checks"]["db"]
    assert db["status"] == "ok"
    assert db["detail"] == "read+write verified"
    assert db["evidence"]["table"] == "sentinel_probe_ping"
    assert db["evidence"]["rows"] == 1


def test_probe_db_factory_awaitable_form(sqlite_factory: async_sessionmaker) -> None:
    async def factory() -> object:
        return sqlite_factory()

    data = _get(_app(llm=False, notify=False, db_session_factory=factory))
    assert data["checks"]["db"]["status"] == "ok"


def test_probe_db_custom_check_overrides_factory(sqlite_factory: async_sessionmaker) -> None:
    async def db_check() -> dict:
        return {"status": "fail", "latency_ms": 0, "detail": "boom", "evidence": {}}

    data = _get(_app(llm=False, notify=False, db_session_factory=sqlite_factory, db_check=db_check))
    assert data["checks"]["db"]["detail"] == "boom"


def test_probe_db_factory_error_fails(sqlite_factory: async_sessionmaker) -> None:
    def broken_factory() -> object:
        raise RuntimeError("pool dead")

    data = _get(_app(llm=False, notify=False, db_session_factory=broken_factory))
    assert data["checks"]["db"]["status"] == "fail"
    assert "pool dead" in data["checks"]["db"]["detail"]
    assert data["overall"] == "fail"


def test_probe_cache_semantics(sqlite_factory: async_sessionmaker) -> None:
    client = _app(llm=False, notify=False, db_session_factory=sqlite_factory)
    first = _get(client)
    second = _get(client)
    fresh = _get(client, fresh=True)
    assert first["cached"] is False
    assert second["cached"] is True
    assert fresh["cached"] is False


def test_build_runner_direct_call() -> None:
    runner = build_probe_runner(app_name="solo", llm=False, notify=False)
    data = asyncio.new_event_loop().run_until_complete(runner(fresh=False))
    assert data["app"] == "solo"
    assert data["overall"] == "ok"
    assert data["checks"]["db"]["status"] == "skipped"


def test_probe_asynccontextmanager_form(sqlite_factory: async_sessionmaker) -> None:
    @asynccontextmanager
    async def factory() -> AsyncIterator[object]:
        async with sqlite_factory() as session:
            yield session

    data = _get(_app(llm=False, notify=False, db_session_factory=factory))
    assert data["checks"]["db"]["status"] == "ok"


def test_probe_llm_ironman_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.ironman.is_ironman_available", lambda: False)
    data = _get(_app(llm=True, notify=False))
    llm = data["checks"]["llm"]
    assert llm["status"] == "fail"
    assert llm["evidence"]["reason"] == "ironman_unavailable"


def test_probe_llm_ping_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Svc:
        async def ask(self, prompt: str, **kwargs: object) -> str:
            assert prompt == "ping"
            assert kwargs.get("max_tokens") == 8
            return "pong"

    monkeypatch.setattr("nexus.ironman.is_ironman_available", lambda: True)
    monkeypatch.setattr("nexus.get_llm_service", lambda: _Svc())
    data = _get(_app(llm=True, notify=False))
    assert data["checks"]["llm"]["status"] == "ok"
    assert data["checks"]["llm"]["evidence"]["reply_head"] == "pong"


def test_probe_active_scheduler_form() -> None:
    class _Active:
        def running(self) -> bool:
            return True

        def list_jobs(self) -> dict[str, str]:
            return {"job-a": "* * * * *"}

    data = _get(_app(llm=False, notify=False, scheduler=_Active()))
    assert data["checks"]["scheduler"]["status"] == "ok"
    assert data["checks"]["scheduler"]["detail"].startswith("running=True")


def test_probe_llm_lazy_init_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def lazy() -> None:
        calls.append("init")

    class _Svc:
        async def ask(self, prompt: str, **kwargs: object) -> str:
            return "pong"

    monkeypatch.setattr("nexus.ironman.is_ironman_available", lambda: True)
    monkeypatch.setattr("nexus.get_llm_service", lambda: _Svc())
    data = _get(_app(llm=True, llm_lazy_init=lazy, notify=False))
    assert data["checks"]["llm"]["status"] == "ok"
    assert calls == ["init"]
    data = _get(_app(llm=True, llm_lazy_init=lazy, notify=False))
    assert data["cached"] is True
    assert calls == ["init"]
