"""探针标准问检查项原语（r239 自 business_probe.py 拆出）。

拆分轴=标准问实现 vs 探针骨架：db/integrity/llm/scheduler 四问的标准
检查项收于此处（notify 问留守 business_probe——其 patch 面
nexus.business_probe.httpx/get_notify_client 钉在骨架命名空间，
教训 #258），探针 runner/缓存/端点注册留 business_probe。
"""
from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Awaitable, Callable, Optional, Protocol, Union

LLM_TIMEOUT_S: float = 20.0
SCHED_TICK_MAX_AGE_S: int = 900
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


def make_integrity_check(factory: ProbeFactory) -> ProbeCheck:
    """SQLite 完整性巡检检查项（PRAGMA quick_check，O(N) 例行档）。

    与 db 问共用业务仓 session factory 但独立成检查名：库腐化（索引损坏类）
    读写探针照常通过，只有完整性扫描能发现（r191 goldenStock 实录）。
    full integrity_check 由 backup 原语在每次备份时执行，此处为 5min 级轻巡检。
    """

    async def _check() -> dict[str, object]:
        from sqlalchemy import text

        started: float = time.monotonic()
        try:
            cm = factory()
            if not isinstance(cm, AbstractAsyncContextManager):
                cm = await cm
            async with cm as db:
                rows = (await db.execute(text("PRAGMA quick_check"))).fetchall()
            result: str = " ".join(str(row[0]) for row in rows)[:160]
            ok: bool = result.startswith("ok")
            return {
                "status": "ok" if ok else "fail",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": "quick_check ok" if ok else f"quick_check: {result}",
                "evidence": {"mode": "quick_check"},
            }
        except Exception as exc:
            return _fail(started, exc)

    return _check


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
    reply: dict[str, object] = await get_llm_service().probe_ping(timeout=LLM_TIMEOUT_S)
    latency: int = int(reply.get("latency_ms") or 0)
    if not reply.get("ok"):
        return {
            "status": "fail",
            "latency_ms": latency,
            "detail": str(reply.get("error") or "llm ping failed"),
            "evidence": {},
        }
    head: str = str(reply.get("reply_head") or "")
    if reply.get("reasoning_only"):
        return {
            "status": "ok",
            "latency_ms": latency,
            "detail": "chat ping ok (reasoning-only reply, 链路活着)",
            "evidence": {"reply_head": head},
        }
    return {
        "status": "ok",
        "latency_ms": latency,
        "detail": "chat ping ok",
        "evidence": {"reply_head": head},
    }


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
    if getattr(scheduler, "running", None) is False:
        gate = getattr(scheduler, "gate", None)
        if gate is not None and not getattr(gate, "allowed", True):
            return {
                "status": "skipped",
                "latency_ms": 0,
                "detail": "scheduler gate refused (SCHEDULER_ENABLED 未开)",
                "evidence": {"reason": "scheduler_gate_refused"},
            }
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
        from nexus.probe_extras import jobs_health

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
