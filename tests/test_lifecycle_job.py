"""register_lifecycle_job 周期清理接线守底（r205 重启驱动清理根治）。

usercenter 生产实录：S29「接线点二选一」下 13 仓 startup-only，清理频率=重启
频率——2 行过期会话躺 5 天 3000+ 轮 watermark degraded。守底锁：cron 注册、
真删透传、dry_run 保守、异常不炸调度、db_path callable 形态。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nexus import lifecycle_job
from nexus.lifecycle import LifecyclePolicy
from nexus.lifecycle_job import register_lifecycle_job

POLICIES = [LifecyclePolicy(table="logs", ttl_days=90, min_age_hours=0)]


def _seed_db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, created_at TEXT)")
    now = datetime.now()
    conn.execute(
        "INSERT INTO logs VALUES (1, ?)",
        ((now - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.execute(
        "INSERT INTO logs VALUES (2, ?)",
        ((now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()
    return path


def _rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    conn.close()
    return n


class FakeScheduler:
    def __init__(self) -> None:
        self.registered: dict[str, tuple] = {}
        self.start_calls = 0
        self.running = False

    def add_cron_job(self, job, job_id: str, expr: str = "", **kwargs):
        self.registered[job_id] = (job, expr)
        return job_id

    def start(self, mode: str | None = None) -> None:
        self.start_calls += 1


def _fake(monkeypatch: pytest.MonkeyPatch) -> FakeScheduler:
    fake = FakeScheduler()
    import nexus.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "get_scheduler", lambda: fake)
    return fake


def test_register_wires_cron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    result = register_lifecycle_job(POLICIES, _seed_db(tmp_path))
    assert result is fake
    job, expr = fake.registered["lifecycle-purge"]
    assert expr == "20 4 * * *"
    assert callable(job)


def test_job_run_deletes_expired_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    path = _seed_db(tmp_path)
    register_lifecycle_job(POLICIES, path, archive_root=tmp_path / "archives")
    job, _ = fake.registered["lifecycle-purge"]
    job()
    assert _rows(path) == 1, "200 天前行应被删，1 天前行保留（TTL 90d）"
    backups = list((tmp_path / "archives").rglob("logs-*.jsonl.gz"))
    assert len(backups) == 1, "删除前必须留 JSONL.gz 备份（fail-closed 护栏）"


def test_job_dry_run_keeps_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    path = _seed_db(tmp_path)
    register_lifecycle_job(POLICIES, path, dry_run=True)
    job, _ = fake.registered["lifecycle-purge"]
    job()
    assert _rows(path) == 2, "dry_run 透传，不动数据"


def test_job_swallows_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    register_lifecycle_job(POLICIES, tmp_path / "missing.db")
    job, _ = fake.registered["lifecycle-purge"]
    job()


def test_job_callable_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    path = _seed_db(tmp_path)
    register_lifecycle_job(POLICIES, lambda: path, archive_root=tmp_path / "archives")
    job, _ = fake.registered["lifecycle-purge"]
    job()
    assert _rows(path) == 1


def test_job_id_and_cron_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    register_lifecycle_job(
        POLICIES, _seed_db(tmp_path), job_id="uc-lifecycle", cron_expr="0 5 * * 1"
    )
    job, expr = fake.registered["uc-lifecycle"]
    assert expr == "0 5 * * 1"
    assert lifecycle_job.LIFECYCLE_JOB_ID == "lifecycle-purge"


def test_auto_start_default_starts_when_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    register_lifecycle_job(POLICIES, _seed_db(tmp_path))
    assert fake.start_calls == 1, "默认带活 job：无调度生命周期的仓单行接线即可用"


def test_auto_start_explicit_false_leaves_to_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    register_lifecycle_job(POLICIES, _seed_db(tmp_path), auto_start=False)
    assert fake.start_calls == 0, "显式 False 交宿主 lifespan start 兜底"


def test_auto_start_noop_when_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
    fake.running = True
    register_lifecycle_job(POLICIES, _seed_db(tmp_path), auto_start=True)
    assert fake.start_calls == 0, "已在运行的 scheduler 不重复 start（幂等）"
