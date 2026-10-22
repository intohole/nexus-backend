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
import nexus.probe_extras as _pe
from nexus import build_probe_runner, register_probe_endpoints
from nexus.probe_extras import record_send_outcome


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
    assert data["schema"] == 2
    assert data["app"] == "demo"
    assert data["overall"] == "ok"
    assert data["cached"] is False
    for name in ("llm", "db", "notify", "scheduler"):
        assert data["checks"][name]["status"] == "skipped"
        assert data["checks"][name]["evidence"]["reason"] == f"no_{name}_consumer"
    assert data["checks"]["watermark"]["status"] == "skipped"
    assert data["checks"]["watermark"]["evidence"]["reason"] == "no_watermark_consumer"


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


def test_probe_llm_lazy_init_error_is_fail_not_5xx() -> None:
    async def broken_lazy() -> None:
        raise RuntimeError("lion down")

    data = _get(_app(llm=True, llm_lazy_init=broken_lazy, notify=False))
    llm = data["checks"]["llm"]
    assert llm["status"] == "fail"
    assert llm["evidence"]["reason"] == "lazy_init_error"
    assert "lion down" in llm["detail"]


def test_probe_v2_watermark_custom_check() -> None:
    async def wm() -> dict:
        return {"status": "degraded", "latency_ms": 3, "detail": "cleanup overdue: t1", "evidence": {}}

    data = _get(_app(llm=False, notify=False, watermark_check=wm))
    assert data["checks"]["watermark"]["status"] == "degraded"
    assert data["overall"] == "degraded"


def test_probe_v2_notify_custom_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def self_check() -> dict:
        return {"status": "ok", "latency_ms": 1, "detail": "self ok", "evidence": {"self": True}}

    data = _get(_app(llm=False, notify=self_check))
    assert data["checks"]["notify"]["status"] == "ok"
    assert data["checks"]["notify"]["evidence"]["self"] is True


def test_probe_v2_send_ledger_ok_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

    class _Client:
        base_url = "http://notify.test"

        def __init__(self, **kwargs: object) -> None: ...

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> None: ...

        async def get(self, url: str) -> "_Resp":
            return _Resp()

    async def _get_resp() -> _Resp:
        return _Resp()

    monkeypatch.setattr("nexus.business_probe.httpx.AsyncClient", _Client)
    monkeypatch.setattr("nexus.business_probe.get_notify_client", lambda: _Client())
    os.environ["SERVICE_TOKEN"] = "t"
    try:
        _pe._send_ledger.pop("demo", None)
        record_send_outcome("demo", ok=True, notify_id=1627, detail="sent")
        data = _get(_app(llm=False, notify=True))
        notify = data["checks"]["notify"]
        assert notify["status"] == "ok"
        assert notify["evidence"]["last_send"]["notify_id"] == "1627"
        assert notify["evidence"]["last_send"]["ok"] is True
    finally:
        os.environ.pop("SERVICE_TOKEN", None)
        _pe._send_ledger.pop("demo", None)


def test_probe_v2_send_ledger_fail_flips_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

    class _Client:
        base_url = "http://notify.test"

        def __init__(self, **kwargs: object) -> None: ...

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> None: ...

        async def get(self, url: str) -> "_Resp":
            return _Resp()

    monkeypatch.setattr("nexus.business_probe.httpx.AsyncClient", _Client)
    monkeypatch.setattr("nexus.business_probe.get_notify_client", lambda: _Client())
    os.environ["SERVICE_TOKEN"] = "t"
    try:
        _pe._send_ledger.pop("demo", None)
        record_send_outcome("demo", ok=False, detail="401 unauthorized")
        data = _get(_app(llm=False, notify=True))
        notify = data["checks"]["notify"]
        assert notify["status"] == "fail"
        assert "last real send failed" in notify["detail"]
        assert data["overall"] == "fail"
    finally:
        os.environ.pop("SERVICE_TOKEN", None)
        _pe._send_ledger.pop("demo", None)


def test_probe_v2_scheduler_per_job_failing() -> None:
    recent: str = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    failing = _FakeScheduler(
        {
            "job-ok": {"next_run_at": "2026-10-12T07:00:00", "failures": 0, "last_success_at": recent},
            "job-dead": {"next_run_at": "2026-10-12T07:00:00", "failures": 7},
        },
        last_tick=recent,
    )
    data = _get(_app(llm=False, notify=False, scheduler=failing))
    sched = data["checks"]["scheduler"]
    assert sched["status"] == "fail"
    assert sched["evidence"]["per_job"]["failing"] == ["job-dead"]
    assert "failing: job-dead" in sched["detail"]


def test_probe_value_check_skipped_by_default() -> None:
    data = _get(_app(llm=False, notify=False))
    assert data["checks"]["value"]["status"] == "skipped"
    assert data["checks"]["value"]["evidence"]["reason"] == "no_value_consumer"


def test_probe_value_check_custom_reports() -> None:
    async def _value() -> dict:
        return {
            "status": "ok",
            "evidence": {"actions_per_active_user_day": 3.2, "window_days": 7},
        }

    data = _get(_app(llm=False, notify=False, value_check=_value))
    assert data["checks"]["value"]["status"] == "ok"
    assert data["checks"]["value"]["evidence"]["actions_per_active_user_day"] == 3.2
