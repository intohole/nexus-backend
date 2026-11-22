"""业务级深度探针骨架（S1 契约 v1→v2 单源）：GET /api/_internal/probe 五问 llm/db/notify/scheduler/watermark。

/health 只证明进程活着，本探针证明业务链路活着。标准问检查项收于
nexus.probe_checks（r239 拆出），本模块保留探针骨架：缓存、runner
装配、端点注册与 notify 问标准实现（其 patch 面
nexus.business_probe.httpx/get_notify_client 钉在本命名空间，教训 #258）。
仓侧仅注入差异化适配（session factory / 调度器对象 / 异构问回调），
无对应消费面的问以显式声明落 skipped（evidence.reason 必填，禁谎报）。
v2（S31）：schema=2——第五问 watermark（probe_extras.make_watermark_check）、
notify 问消费投递记账（record_send_outcome）、scheduler 问 per-job 分级。
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional, Union

import httpx
from fastapi import APIRouter, Depends, FastAPI

from nexus.internal_endpoints import require_service_token
from nexus.notify import get_notify_client
from nexus.probe_checks import (
    LLM_TIMEOUT_S as LLM_TIMEOUT_S,
)
from nexus.probe_checks import (
    PROBE_TABLE as PROBE_TABLE,
)
from nexus.probe_checks import (
    SCHED_TICK_MAX_AGE_S as SCHED_TICK_MAX_AGE_S,
)
from nexus.probe_checks import (
    ActiveSchedulerLike,
    ProbeCheck,
    ProbeFactory,
    SchedulerLike,
    _dispatch_scheduler_check,
    _fail,
    _skip,
    _standard_db_check,
    _standard_llm_check,
    make_integrity_check,
)
from nexus.probe_checks import (
    probe_scheduler as probe_scheduler,
)
from nexus.probe_extras import merge_last_send_evidence

PROBE_SCHEMA: int = 2
CACHE_TTL_S: int = 300
LLM_TTL_S: int = 600
NOTIFY_TIMEOUT_S: float = 10.0

IntegritySetting = Union[bool, ProbeCheck, None]

_cache: dict[str, tuple[float, dict[str, object]]] = {}


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
    value_check: Optional[ProbeCheck] = None,
    backup_check: Optional[ProbeCheck] = None,
    deps_check: Optional[ProbeCheck] = None,
    memory_check: Optional[ProbeCheck] = None,
    integrity: IntegritySetting = None,
) -> Callable[[bool], Awaitable[dict[str, object]]]:
    _prefix: str = f"{app_name}:"

    async def _db_via_factory() -> dict[str, object]:
        return await _standard_db_check(db_session_factory)

    def _integrity_via_factory() -> ProbeCheck:
        assert db_session_factory is not None
        return make_integrity_check(db_session_factory)

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
        val_fn: Optional[ProbeCheck] = value_check
        bk_fn: Optional[ProbeCheck] = backup_check
        dep_fn: Optional[ProbeCheck] = deps_check
        mem_fn: Optional[ProbeCheck] = memory_check
        if integrity is None:
            ig_fn: Optional[ProbeCheck] = _integrity_via_factory() if db_session_factory else None
        elif integrity is False:
            ig_fn = None
        else:
            ig_fn = integrity

        for name, fn in (
            ("db", db_fn),
            ("notify", notify_fn),
            ("scheduler", sched_fn),
            ("watermark", wm_fn),
            ("value", val_fn),
            ("backup", bk_fn),
            ("deps", dep_fn),
            ("memory", mem_fn),
            ("integrity", ig_fn),
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
    value_check: Optional[ProbeCheck] = None,
    backup_check: Optional[ProbeCheck] = None,
    deps_check: Optional[ProbeCheck] = None,
    memory_check: Optional[ProbeCheck] = None,
    integrity: IntegritySetting = None,
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
        value_check=value_check,
        backup_check=backup_check,
        deps_check=deps_check,
        memory_check=memory_check,
        integrity=integrity,
    )
    router: APIRouter = APIRouter(
        prefix="/api/_internal", tags=["internal-probe"], dependencies=[Depends(require_service_token)]
    )

    @router.get("/probe")
    async def deep_probe(fresh: bool = False) -> dict[str, object]:
        return await run_probe(fresh=fresh)

    app.include_router(router)
