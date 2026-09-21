"""crontab 通用组件单元测试."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from nexus.crontab import CronScheduler, next_run_at
from nexus.scheduler import get_scheduler

CHINA = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 9, 14, 10, 30)


def test_next_run_at_daily() -> None:
    nxt = next_run_at("0 9 * * *", BASE)
    assert nxt.year == 2026 and nxt.month == 9 and nxt.day == 15 and nxt.hour == 9 and nxt.minute == 0


def test_next_run_at_same_day_future() -> None:
    nxt = next_run_at("30 11 * * *", BASE)
    assert nxt.day == 14 and nxt.hour == 11 and nxt.minute == 30


def test_next_run_at_weekday() -> None:
    nxt = next_run_at("0 9 * * 1-5", BASE)
    assert nxt.weekday() < 5 and nxt.hour == 9


def test_next_run_at_interval() -> None:
    nxt = next_run_at("*/5 * * * *", BASE)
    assert nxt.minute == 35


def test_next_run_at_invalid() -> None:
    assert next_run_at("", BASE) is None
    assert next_run_at("bad expr", BASE) is None


def test_next_run_at_base_aware() -> None:
    base_aware = datetime(2026, 9, 14, 2, 30, tzinfo=CHINA)
    nxt = next_run_at("0 9 * * *", base_aware)
    assert nxt.hour == 9 and nxt.day == 14


def test_next_run_at_utc_tz() -> None:
    nxt = next_run_at("0 9 * * *", BASE, tz=ZoneInfo("UTC"))
    assert nxt.hour == 9


def test_scheduler_cron_job_fires() -> None:
    scheduler = CronScheduler(tick_seconds=1)
    fired: list[str] = []

    async def job() -> None:
        fired.append("run")

    async def run() -> None:
        scheduler.add_cron_job("test", "* * * * * *", job)
        scheduler.start()
        await asyncio.sleep(1.2)
        await scheduler.stop()

    asyncio.run(run())
    assert "run" in fired


def test_scheduler_scanner_runs() -> None:
    scheduler = CronScheduler(tick_seconds=1)
    scans: list[str] = []

    async def scan() -> None:
        scans.append("scan")

    async def run() -> None:
        scheduler.register_scanner("test", scan)
        scheduler.start()
        await asyncio.sleep(1.2)
        await scheduler.stop()

    asyncio.run(run())
    assert "scan" in scans


def test_scheduler_scanner_error_isolated() -> None:
    scheduler = CronScheduler(tick_seconds=1)
    scans: list[str] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> None:
        scans.append("good")

    async def run() -> None:
        scheduler.register_scanner("bad", bad)
        scheduler.register_scanner("good", good)
        scheduler.start()
        await asyncio.sleep(1.2)
        await scheduler.stop()

    asyncio.run(run())
    assert "good" in scans


def test_scheduler_cron_job_error_isolated() -> None:
    scheduler = CronScheduler(tick_seconds=1)
    fired: list[str] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> None:
        fired.append("good")

    async def run() -> None:
        scheduler.add_cron_job("bad", "* * * * * *", bad)
        scheduler.add_cron_job("good", "* * * * * *", good)
        scheduler.start()
        await asyncio.sleep(1.2)
        await scheduler.stop()

    asyncio.run(run())
    assert "good" in fired


def test_nexus_scheduler_add_cron_expr() -> None:
    scheduler = get_scheduler()
    job_id = scheduler.add_cron_job(
        lambda: None,
        "test-expr",
        expr="0 9 * * 1-5",
        timezone="Asia/Shanghai",
    )
    assert job_id == "test-expr"
    assert scheduler.list_jobs().get("test-expr") == "cron"
    scheduler.remove_job(job_id)


def test_nexus_scheduler_add_cron_params_backward_compat() -> None:
    scheduler = get_scheduler()
    job_id = scheduler.add_cron_job(lambda: None, "test-params", hour=9, minute=0)
    assert scheduler.list_jobs().get(job_id) == "cron"
    scheduler.remove_job(job_id)


if __name__ == "__main__":
    sys_exit = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                sys_exit = 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(sys_exit)
