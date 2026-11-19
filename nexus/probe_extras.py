"""业务探针契约 v2 扩展（S31）：投递记账单源+表行数水位+per-job 心跳分级。

v1 四问（llm/db/notify/scheduler）见 business_probe.py；本模块只承载 v2 新增面，
全部向后兼容：不接线的仓探针多一个 skipped(watermark)，行为零漂移。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from nexus.lifecycle import LifecyclePolicy, lifecycle_status

SEND_STALE_S: int = 7 * 24 * 3600
JOB_FAILURES_FAIL: int = 5
OVERDUE_FAIL_ROWS: int = 1000

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
            overdue_rows: int = sum(
                int(v.get("overdue") or 0) for v in report.values() if isinstance(v, dict)
            )
            hard: bool = overdue_rows > OVERDUE_FAIL_ROWS
            status: str = ("fail" if hard else "degraded") if overdue else "ok"
            detail: str = f"{len(report)} tables watched"
            if overdue:
                detail += f", cleanup overdue: {','.join(overdue[:3])} ({overdue_rows} rows)"
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
    "make_backup_freshness_check",
    "make_deps_drift_check",
    "SEND_STALE_S",
    "JOB_FAILURES_FAIL",
]


def make_backup_freshness_check(
    backup_dir: str | Path,
    prefix: str,
    max_age_hours: float = 24.0,
) -> "object":
    """「备份产物新鲜度」便利构造器（r223 DE3）：只读包 backup_drill.backup_freshness。"""
    from nexus.backup_drill import backup_freshness

    async def _check() -> dict[str, object]:
        started: float = time.monotonic()
        report: dict[str, object] = await asyncio.to_thread(
            backup_freshness, Path(backup_dir), prefix, max_age_hours
        )
        status: str = "ok" if report.get("ok") else "fail"
        detail: str = (
            f"latest={report.get('latest')} age_hours={report.get('age_hours')}"
            if report.get("ok")
            else f"stale or missing backup: {report.get('error') or report.get('latest')} age_hours={report.get('age_hours')}"
        )
        return {
            "status": status,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": detail[:160],
            "evidence": report,
        }

    return _check


_DEPS_SKIP_DIRS: set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "vendor", "static",
}


def _parse_req_lines(work_dir: Path) -> list[str]:
    specs: list[str] = []
    base_depth: int = len(work_dir.resolve().parts)
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in _DEPS_SKIP_DIRS]
        if len(Path(root).resolve().parts) - base_depth > 2:
            dirs[:] = []
            continue
        if "requirements.txt" not in files:
            continue
        try:
            for line in (Path(root) / "requirements.txt").read_text(
                encoding="utf-8", errors="ignore"
            ).splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "-", "git+", "http")) and line not in specs:
                    specs.append(line)
        except OSError:
            continue
    return specs


def _req_import_name(spec: str) -> str:
    name: str = spec.split(";")[0].split("#")[0].strip()
    if " @ " in name:
        name = name.split(" @ ", 1)[0]
    name = re.sub(r"\[[^\]]*\]", "", name)
    for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        name = name.split(sep)[0]
    return name.strip().replace("-", "_").replace(".", "_")


def _req_pinned(spec: str) -> str:
    m = re.search(r"==\s*([0-9][^;\s]*)", spec)
    return m.group(1) if m else ""


def make_deps_drift_check(
    work_dir: str | Path,
) -> "object":
    """「依赖漂移」便利构造器（S173）：声明面 requirements vs 实装面对表。

    口径与 miniDeploy app_deps.check_app_requirements 同源（发现深度≤2、文件名
    requirements.txt、==钉版、packaging.Version 相等）；分列字段对齐 S172
    diagnose（missing/deps_drifted）。missing→fail（环境残缺）、仅钉版漂移→
    degraded（包在版本不符）、全满足→ok、无声明面→skipped（禁谎报）。
    """
    from importlib import metadata

    def _report() -> dict[str, object]:
        specs: list[str] = _parse_req_lines(Path(work_dir))
        if not specs:
            return {
                "status": "skipped",
                "latency_ms": 0,
                "detail": "no requirements declared",
                "evidence": {"reason": "no_requirements_declared"},
            }
        missing: list[str] = []
        drifted: list[str] = []
        for spec in specs:
            import_name: str = _req_import_name(spec)
            if not import_name or import_name in {"http", "git", "file", "pip", "setuptools"}:
                continue
            dist_name: str = re.sub(r"[-_.]+", "-", import_name).lower()
            try:
                installed: str = metadata.version(import_name)
            except Exception:
                try:
                    installed = metadata.version(dist_name)
                except Exception:
                    installed = ""
            pinned: str = _req_pinned(spec)
            if not installed:
                missing.append(spec)
            elif pinned and not _version_eq(pinned, installed):
                drifted.append(f"{import_name} {installed}→{pinned}")
        evidence: dict[str, object] = {
            "missing": missing,
            "drifted": drifted,
            "declared": len(specs),
        }
        if missing:
            status, detail = "fail", f"{len(missing)} missing deps"
        elif drifted:
            status, detail = "degraded", f"{len(drifted)} pinned drift"
        else:
            status, detail = "ok", f"{len(specs)} declared, all satisfied"
        return {
            "status": status,
            "latency_ms": 0,
            "detail": detail,
            "evidence": evidence,
        }

    async def _check() -> dict[str, object]:
        started: float = time.monotonic()
        try:
            out: dict[str, object] = await asyncio.to_thread(_report)
        except Exception as exc:
            return {
                "status": "fail",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"[:160],
                "evidence": {},
            }
        out["latency_ms"] = int((time.monotonic() - started) * 1000)
        return out

    return _check


def _version_eq(declared: str, installed: str) -> bool:
    try:
        from packaging.version import Version

        return Version(declared) == Version(installed)
    except Exception:
        return declared.strip() == installed.strip()
