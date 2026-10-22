"""业务级深度探针（S1 契约 v1→v2 单源）：GET /api/_internal/probe 五问 llm/db/notify/scheduler/watermark。

/health 只证明进程活着，本探针证明业务链路活着。问的标准实现收于此处，
仓侧仅注入差异化适配（session factory / 调度器对象 / 异构问回调），
无对应消费面的问以显式声明落 skipped（evidence.reason 必填，禁谎报）。
v2（S31）：schema=2——第五问 watermark（probe_extras.make_watermark_check）、
notify 问消费投递记账（record_send_outcome）、scheduler 问 per-job 分级。
"""
from __future__ import annotations

import os
import time
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol, Union

import httpx
from fastapi import APIRouter, Depends, FastAPI
from nexus.internal_endpoints import require_service_token
from nexus.notify import get_notify_client
from nexus.probe_extras import jobs_health, merge_last_send_evidence

PROBE_SCHEMA: int = 2
CACHE_TTL_S: int = 300
LLM_TTL_S: int = 600
SCHED_TICK_MAX_AGE_S: int = 900
LLM_TIMEOUT_S: float = 20.0
NOTIFY_TIMEOUT_S: float = 10.0
PROBE_TABLE: str = "sentinel_probe_ping"

ProbeCheck = Callable[[], Awaitable[dict[str, object]]]
ProbeFactory = Callable[
    [],
    Union[AbstractAsyncContextManager[object], Awaitable[AbstractAsyncContextManager[object]]],
]


class SchedulerLike(Protocol):
    def status(self) -> dict[str, object]: ...

    def ensure_alive(self) -> bool: ...


class ActiveSchedulerLike(Protocol):
    def running(self) -> bool: ...

    def list_jobs(self) -> dict[str, str]: ...

_cache: dict[str, tuple[float, dict[str, object]]] = {}


def _fail(started: float, exc: Exception) -> dict[str, object]:
    return {
        "status": "fail",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "detail": f"{type(exc).__name__}: {exc}"[:160],
        "evidence": {},
    }


def _skip(reason: str) -> dict[str, object]:
    return {"status": "skipped", "latency_ms": None, "detail": reason, "evidence": {"reason": reason}}


async def _standard_db_check(factory: ProbeFactory) -> dict[str, object]:
    from sqlalchemy import text

    started: float = time.monotonic()
    try:
        cm = factory()
        if not isinstance(cm, AbstractAsyncContextManager):
            cm = await cm
        async with cm as db:
            await db.execute(text(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (ts TEXT NOT NULL)"))
            await db.execute(text(f"DELETE FROM {PROBE_TABLE}"))
            await db.execute(
                text(f"INSERT INTO {PROBE_TABLE} (ts) VALUES (:ts)"),
                {"ts": datetime.now().astimezone().isoformat(timespec="seconds")},
            )
            await db.commit()
            row = (await db.execute(text(f"SELECT COUNT(*) FROM {PROBE_TABLE}"))).scalar()
        return {
            "status": "ok",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": "read+write verified",
            "evidence": {"table": PROBE_TABLE, "rows": int(row or 0)},
        }
    except Exception as exc:
        return _fail(started, exc)


async def _standard_llm_check() -> dict[str, object]:
    from nexus import get_llm_service
    from nexus.ironman import is_ironman_available

    if not is_ironman_available():
        return {
            "status": "fail",
            "latency_ms": None,
            "detail": "ironman not available (LLM 未配置或启动失败)",
            "evidence": {"reason": "ironman_unavailable"},
        }
    started: float = time.monotonic()
    try:
        reply: str = await get_llm_service().ask("ping", max_tokens=8, timeout=LLM_TIMEOUT_S)
        latency: int = int((time.monotonic() - started) * 1000)
        if not isinstance(reply, str) or not reply.strip():
            return {
                "status": "fail",
                "latency_ms": latency,
                "detail": "empty llm reply",
                "evidence": {"reply_type": type(reply).__name__},
            }
        return {
            "status": "ok",
            "latency_ms": latency,
            "detail": "chat ping ok",
            "evidence": {"reply_head": reply.strip()[:16]},
        }
    except Exception as exc:
        return _fail(started, exc)


async def _standard_notify_check(app_name: str = "") -> dict[str, object]:
    started: float = time.monotonic()
    client = get_notify_client()
    auth_configured: bool = bool(os.environ.get("SERVICE_TOKEN", "")) or bool(
        os.environ.get("LION__NAMESPACE") or os.environ.get("LION_NAMESPACE", "")
    )
    try:
        async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT_S) as http:
            resp: httpx.Response = await http.get(f"{client.base_url.rstrip('/')}/health")
        reachable: bool = resp.status_code == 200
        status: str = "ok" if reachable and auth_configured else "fail"
        detail: str = "center reachable" if reachable else f"center http {resp.status_code}"
        if reachable and not auth_configured:
            detail = "center reachable but service token not configured"
        result: dict[str, object] = {
            "status": status,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": detail,
            "evidence": {
                "center_reachable": reachable,
                "auth_configured": auth_configured,
                "center_url": client.base_url,
            },
        }
    except Exception as exc:
        result = _fail(started, exc)
        result["evidence"] = {"center_reachable": False, "auth_configured": auth_configured}
    if app_name:
        return merge_last_send_evidence(app_name, result)
    return result


def _active_scheduler_check(scheduler: ActiveSchedulerLike) -> dict[str, object]:
    started: float = time.monotonic()
    try:
        running: bool = bool(scheduler.running)
        jobs: dict[str, str] = scheduler.list_jobs()
        ok: bool = running and bool(jobs)
        detail: str = f"running={running}, {len(jobs)} jobs"
        if jobs:
            detail += f" ({','.join(list(jobs.keys())[:3])})"
        return {
            "status": "ok" if ok else "fail",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": detail,
            "evidence": {"running": running, "jobs": {k: str(v)[:60] for k, v in list(jobs.items())[:5]}},
        }
    except Exception as exc:
        return _fail(started, exc)


def probe_scheduler(scheduler: Union[SchedulerLike, ActiveSchedulerLike]) -> dict[str, object]:
    if hasattr(scheduler, "ensure_alive"):
        return _standard_scheduler_check(scheduler)  # type: ignore[arg-type]
    return _active_scheduler_check(scheduler)  # type: ignore[arg-type]


def _dispatch_scheduler_check(
    scheduler: Union[SchedulerLike, ActiveSchedulerLike],
) -> dict[str, object]:
    return probe_scheduler(scheduler)


def _standard_scheduler_check(scheduler: SchedulerLike) -> dict[str, object]:
    started: float = time.monotonic()
    try:
        status: dict[str, object] = scheduler.status()
        alive: bool = not scheduler.ensure_alive()
        last_tick: Optional[str] = status.get("last_tick_at") if isinstance(status, dict) else None
        jobs_raw: object = status.get("jobs") if isinstance(status, dict) else None
        jobs: dict[str, object] = jobs_raw if isinstance(jobs_raw, dict) else {}
        never_scheduled: list[str] = [
            jid for jid, meta in jobs.items()
            if not isinstance(meta, dict) or meta.get("next_run_at") in (None, "None", "")
        ]
        tick_age_s: Optional[float] = None
        if last_tick:
            try:
                tick_dt: datetime = datetime.fromisoformat(str(last_tick))
                tick_age_s = max(0.0, (datetime.now() - tick_dt).total_seconds())
            except ValueError:
                tick_age_s = None
        ok: bool = alive and not never_scheduled and (
            tick_age_s is not None and tick_age_s <= SCHED_TICK_MAX_AGE_S
        )
        per_job: dict[str, object] = jobs_health(jobs)
        failing_jobs: list[str] = list(per_job.get("failing") or [])
        if failing_jobs:
            ok = False
        detail: str = (
            f"last tick {int(tick_age_s)}s ago, {len(jobs)} jobs"
            if tick_age_s is not None
            else f"alive={alive}, jobs={len(jobs)}"
        )
        if never_scheduled:
            detail += f", never-scheduled: {','.join(never_scheduled[:3])}"
        if failing_jobs:
            detail += f", failing: {','.join(failing_jobs[:3])}"
        return {
            "status": "ok" if ok else "fail",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": detail,
            "evidence": {
                "loop_alive": alive,
                "last_tick_at": last_tick,
                "tick_age_s": tick_age_s,
                "never_scheduled": never_scheduled,
                "per_job": per_job,
            },
        }
    except Exception as exc:
        return _fail(started, exc)


def _cached(key: str, ttl: int) -> Optional[dict[str, object]]:
    hit: Optional[tuple[float, dict[str, object]]] = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _store(key: str, value: dict[str, object]) -> dict[str, object]:
    _cache[key] = (time.monotonic(), value)
    return value


async def _llm_with_lazy_init(lazy: Optional[Callable[[], Awaitable[None]]]) -> dict[str, object]:
    if lazy is not None:
        try:
            await lazy()
        except Exception as exc:
            return {
                "status": "fail",
                "latency_ms": None,
                "detail": f"lazy init failed: {type(exc).__name__}: {exc}"[:160],
                "evidence": {"reason": "lazy_init_error"},
            }
    return await _standard_llm_check()


def build_probe_runner(
    *,
    app_name: str,
    db_session_factory: Optional[ProbeFactory] = None,
    db_check: Optional[ProbeCheck] = None,
    llm: Union[bool, ProbeCheck] = True,
    llm_lazy_init: Optional[Callable[[], Awaitable[None]]] = None,
    notify: Union[bool, ProbeCheck] = True,
    scheduler: Optional[Union[SchedulerLike, ActiveSchedulerLike]] = None,
    scheduler_check: Optional[ProbeCheck] = None,
    watermark_check: Optional[ProbeCheck] = None,
) -> Callable[[bool], Awaitable[dict[str, object]]]:
    _prefix: str = f"{app_name}:"

    async def _db_via_factory() -> dict[str, object]:
        return await _standard_db_check(db_session_factory)

    def _sched_via_obj() -> dict[str, object]:
        return _dispatch_scheduler_check(scheduler)

    async def _notify_standard() -> dict[str, object]:
        return await _standard_notify_check(app_name)

    async def run_probe(fresh: bool = False) -> dict[str, object]:
        checks: dict[str, dict[str, object]] = {}
        cache_hits: int = 0

        llm_cached: Optional[dict[str, object]] = None if fresh else _cached(_prefix + "llm", LLM_TTL_S)
        if llm_cached is not None:
            checks["llm"] = llm_cached
            cache_hits += 1
        elif isinstance(llm, bool):
            checks["llm"] = _store(
                _prefix + "llm",
                await _llm_with_lazy_init(llm_lazy_init) if llm else _skip("no_llm_consumer"),
            )
        else:
            checks["llm"] = _store(_prefix + "llm", await llm())

        db_fn: Optional[ProbeCheck] = db_check or (_db_via_factory if db_session_factory else None)
        notify_fn: Optional[ProbeCheck] = (
            _notify_standard if notify is True else (notify if notify else None)
        )
        sched_fn: Optional[ProbeCheck] = scheduler_check or (_sched_via_obj if scheduler else None)
        wm_fn: Optional[ProbeCheck] = watermark_check

        for name, fn in (
            ("db", db_fn),
            ("notify", notify_fn),
            ("scheduler", sched_fn),
            ("watermark", wm_fn),
        ):
            if fn is None:
                checks[name] = _skip(f"no_{name}_consumer")
                continue
            if not fresh:
                cached: Optional[dict[str, object]] = _cached(_prefix + name, CACHE_TTL_S)
                if cached is not None:
                    checks[name] = cached
                    cache_hits += 1
                    continue
            outcome: Union[dict[str, object], Awaitable[dict[str, object]]] = fn()
            if not isinstance(outcome, dict):
                outcome = await outcome
            checks[name] = _store(_prefix + name, outcome)

        rank: dict[str, int] = {"ok": 0, "skipped": 0, "unknown": 1, "degraded": 2, "fail": 3}
        worst: str = "ok"
        for check in checks.values():
            if rank.get(str(check.get("status")), 1) > rank.get(worst, 1):
                worst = str(check.get("status"))
        return {
            "schema": PROBE_SCHEMA,
            "app": app_name,
            "probe_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cached": cache_hits > 0,
            "overall": worst,
            "checks": checks,
        }

    return run_probe


def register_probe_endpoints(
    app: FastAPI,
    *,
    app_name: str,
    db_session_factory: Optional[ProbeFactory] = None,
    db_check: Optional[ProbeCheck] = None,
    llm: Union[bool, ProbeCheck] = True,
    llm_lazy_init: Optional[Callable[[], Awaitable[None]]] = None,
    notify: Union[bool, ProbeCheck] = True,
    scheduler: Optional[Union[SchedulerLike, ActiveSchedulerLike]] = None,
    scheduler_check: Optional[ProbeCheck] = None,
    watermark_check: Optional[ProbeCheck] = None,
) -> None:
    run_probe: Callable[[bool], Awaitable[dict[str, object]]] = build_probe_runner(
        app_name=app_name,
        db_session_factory=db_session_factory,
        db_check=db_check,
        llm=llm,
        llm_lazy_init=llm_lazy_init,
        notify=notify,
        scheduler=scheduler,
        scheduler_check=scheduler_check,
        watermark_check=watermark_check,
    )
    router: APIRouter = APIRouter(
        prefix="/api/_internal", tags=["internal-probe"], dependencies=[Depends(require_service_token)]
    )

    @router.get("/probe")
    async def deep_probe(fresh: bool = False) -> dict[str, object]:
        return await run_probe(fresh=fresh)

    app.include_router(router)
