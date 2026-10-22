"""业务探针契约 v2 扩展（S31）：投递记账单源+表行数水位+per-job 心跳分级。

v1 四问（llm/db/notify/scheduler）见 business_probe.py；本模块只承载 v2 新增面，
全部向后兼容：不接线的仓探针多一个 skipped(watermark)，行为零漂移。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from nexus.lifecycle import LifecyclePolicy, lifecycle_status

SEND_STALE_S: int = 7 * 24 * 3600
JOB_FAILURES_FAIL: int = 5

_send_ledger: dict[str, dict[str, object]] = {}


def record_send_outcome(
    app_name: str,
    *,
    ok: bool,
    notify_id: object = None,
    detail: str = "",
    at: Optional[datetime] = None,
) -> None:
    """v2 投递记账：应用侧真实 send 后调用（notify_id=notifyCenter 落库回执，非函数返回值）。"""
    _send_ledger[app_name] = {
        "notify_id": None if notify_id is None else str(notify_id),
        "ok": bool(ok),
        "detail": detail[:120],
        "at": (at or datetime.now().astimezone()).isoformat(timespec="seconds"),
    }


def last_send_outcome(app_name: str) -> Optional[dict[str, object]]:
    return _send_ledger.get(app_name)


def merge_last_send_evidence(app_name: str, check: dict[str, object]) -> dict[str, object]:
    """notify 问消费记账：最近一次真实投递失败 → 整体 fail；无记账保持可达性判定。"""
    ledger: Optional[dict[str, object]] = last_send_outcome(app_name)
    evidence: dict[str, object] = dict(check.get("evidence") or {})
    if ledger is None:
        evidence["last_send"] = {"recorded": False}
        check["evidence"] = evidence
        return check
    evidence["last_send"] = ledger
    check["evidence"] = evidence
    if check.get("status") == "ok" and not ledger.get("ok"):
        check["status"] = "fail"
        check["detail"] = f"last real send failed: {ledger.get('detail') or 'no detail'}"[:160]
    return check


def classify_jobs(jobs: dict[str, object]) -> dict[str, str]:
    """per-job 三态分级：ok / never_scheduled / failing（连败≥JOB_FAILURES_FAIL）。"""
    out: dict[str, str] = {}
    for jid, meta in jobs.items():
        if not isinstance(meta, dict):
            out[jid] = "ok"
            continue
        if meta.get("next_run_at") in (None, "None", ""):
            out[jid] = "never_scheduled"
        elif int(meta.get("failures") or 0) >= JOB_FAILURES_FAIL:
            out[jid] = "failing"
        else:
            out[jid] = "ok"
    return out


def jobs_health(jobs: dict[str, object]) -> dict[str, object]:
    """v2 per-job 健康：分级+last_success_at 上桌（从未成功但已调度的 job 单列）。"""
    classes: dict[str, str] = classify_jobs(jobs)
    no_success: list[str] = [
        jid for jid, meta in jobs.items()
        if isinstance(meta, dict) and not meta.get("last_success_at")
        and classes.get(jid) != "never_scheduled"
    ]
    return {
        "classes": classes,
        "failing": [j for j, c in classes.items() if c == "failing"],
        "never_scheduled": [j for j, c in classes.items() if c == "never_scheduled"],
        "no_success_yet": no_success,
        "last_success_at": {
            jid: meta.get("last_success_at")
            for jid, meta in jobs.items()
            if isinstance(meta, dict)
        },
    }


def make_watermark_check(
    db_path: str | Path,
    policies: Sequence[LifecyclePolicy],
) -> "object":
    """第五问「表行数水位」便利构造器：只读包 lifecycle_status，返回 async 检查函数。"""

    async def _check() -> dict[str, object]:
        started: float = time.monotonic()
        try:
            report: dict[str, dict[str, object]] = await asyncio.to_thread(
                lifecycle_status, db_path, policies
            )
            errors: list[str] = [t for t, v in report.items() if isinstance(v, dict) and v.get("error")]
            overdue: list[str] = [
                t for t, v in report.items()
                if isinstance(v, dict) and int(v.get("overdue") or 0) > 0
            ]
            if errors:
                return {
                    "status": "fail",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "detail": f"watch error: {','.join(errors[:3])}"[:160],
                    "evidence": {"tables": report},
                }
            status: str = "degraded" if overdue else "ok"
            detail: str = f"{len(report)} tables watched"
            if overdue:
                detail += f", cleanup overdue: {','.join(overdue[:3])}"
            return {
                "status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": detail,
                "evidence": {"tables": report},
            }
        except Exception as exc:
            return {
                "status": "fail",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"[:160],
                "evidence": {},
            }

    return _check


__all__ = [
    "record_send_outcome",
    "last_send_outcome",
    "merge_last_send_evidence",
    "classify_jobs",
    "jobs_health",
    "make_watermark_check",
    "SEND_STALE_S",
    "JOB_FAILURES_FAIL",
]
