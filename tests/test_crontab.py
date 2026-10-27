"""crontab 通用组件单元测试."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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


def test_scheduler_fire_timeout_does_not_freeze_loop() -> None:
    """挂死任务被 wait_for 打断且节拍推进——loop 不被单任务冻结，其他任务继续。"""
    scheduler = CronScheduler(tick_seconds=1, fire_timeout=0.5)
    done: list[str] = []

    async def hanging() -> None:
        await asyncio.sleep(30)

    async def quick() -> None:
        done.append("quick")

    async def run() -> None:
        scheduler.add_cron_job("hang", "* * * * * *", hanging)
        scheduler.add_cron_job("quick", "* * * * * *", quick)
        scheduler.start()
        await asyncio.sleep(3)
        await scheduler.stop()

    asyncio.run(run())
    assert "quick" in done
    hang = scheduler._cron_jobs["hang"]
    assert hang["next_run_at"] is not None and scheduler._running_jobs == set()


def test_scheduler_no_reentrant_fire() -> None:
    """上一轮未完成时同任务不重入：并发进入数不随 tick 叠加。"""
    scheduler = CronScheduler(tick_seconds=1)
    enters: list[int] = []

    async def slow() -> None:
        enters.append(1)
        await asyncio.sleep(2)

    async def run() -> None:
        scheduler.add_cron_job("slow", "* * * * * *", slow)
        scheduler.start()
        await asyncio.sleep(3)
        await scheduler.stop()

    asyncio.run(run())
    assert 0 < len(enters) <= 2


def test_scheduler_ensure_alive_and_status() -> None:
    scheduler = CronScheduler(tick_seconds=1)

    async def job() -> None:
        pass

    async def run() -> None:
        scheduler.add_cron_job("probe", "* * * * * *", job)
        assert scheduler.ensure_alive() is True
        await asyncio.sleep(0.2)
        assert scheduler.ensure_alive() is False
        st = scheduler.status()
        assert st["alive"] is True
        assert st["last_tick_at"] is not None
        assert st["jobs"]["probe"]["expr"] == "* * * * * *"
        assert st["jobs"]["probe"]["next_run_at"]
        scheduler._task.cancel()
        try:
            await scheduler._task
        except asyncio.CancelledError:
            pass
        assert scheduler.ensure_alive() is True
        await scheduler.stop()

    asyncio.run(run())


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


def test_add_cron_job_rejects_sync_func() -> None:
    """同步函数注册必须 fail-fast：运行时才炸是 aiPet r11 每 30s ERROR 风暴的根因。"""
    scheduler = CronScheduler(tick_seconds=1)

    def sync_job() -> None:
        pass

    try:
        scheduler.add_cron_job("sync", "* * * * * *", sync_job)
    except TypeError:
        pass
    else:
        raise AssertionError("sync handler should be rejected at registration")
    assert "sync" not in scheduler._cron_jobs


def test_failed_job_backoff_is_exponential() -> None:
    """失败任务指数退避：不再原样保持过期 next_run_at（每 tick 重试风暴的根因）。"""
    scheduler = CronScheduler(tick_seconds=30)
    calls: list[int] = []

    async def bad() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    async def run() -> None:
        scheduler.add_cron_job("bad", "* * * * * *", bad)
        job = scheduler._cron_jobs["bad"]
        for expected_backoff in (30.0, 60.0, 120.0):
            before = datetime.now(CHINA).replace(tzinfo=None)
            job["next_run_at"] = before - timedelta(seconds=1)
            await scheduler._maybe_fire("bad", job)
            delta = (job["next_run_at"] - before).total_seconds()
            assert expected_backoff - 1.0 <= delta <= expected_backoff + 1.0
        assert job["failures"] == 3
        assert "boom" in str(job["last_error"])
        st = scheduler.status()
        assert st["jobs"]["bad"]["failures"] == 3
        assert "boom" in str(st["jobs"]["bad"]["last_error"])

    asyncio.run(run())


def test_job_success_resets_failure_state() -> None:
    """失败后成功：failures 归零、last_error 清空、next_run_at 回归表达式节奏。"""
    scheduler = CronScheduler(tick_seconds=30)
    calls: list[str] = []

    async def flaky() -> None:
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def run() -> None:
        scheduler.add_cron_job("flaky", "*/5 * * * * *", flaky)
        job = scheduler._cron_jobs["flaky"]
        job["next_run_at"] = datetime.now(CHINA).replace(tzinfo=None) - timedelta(seconds=1)
        await scheduler._maybe_fire("flaky", job)
        assert job["failures"] == 1
        job["next_run_at"] = datetime.now(CHINA).replace(tzinfo=None) - timedelta(seconds=1)
        await scheduler._maybe_fire("flaky", job)
        assert calls == ["run", "run"]
        assert job["failures"] == 0
        assert job["last_error"] is None
        assert job["next_run_at"] > datetime.now(CHINA).replace(tzinfo=None)

    asyncio.run(run())


def test_backed_off_job_does_not_refire_immediately() -> None:
    """退避窗口内同任务不重触发：next_run_at 已推进到未来即静默等待。"""
    scheduler = CronScheduler(tick_seconds=30)
    calls: list[int] = []

    async def bad() -> None:
        calls.append(1)
        raise RuntimeError("boom")

    async def run() -> None:
        scheduler.add_cron_job("bad", "* * * * * *", bad)
        job = scheduler._cron_jobs["bad"]
        job["next_run_at"] = datetime.now(CHINA).replace(tzinfo=None) - timedelta(seconds=1)
        await scheduler._maybe_fire("bad", job)
        assert calls == [1]
        await scheduler._maybe_fire("bad", job)
        assert calls == [1]

    asyncio.run(run())


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


def test_add_cron_job_bad_expr_logs_error(caplog) -> None:
    import logging

    scheduler = CronScheduler(tick_seconds=1)

    async def job() -> None:
        return None

    with caplog.at_level(logging.ERROR, logger="nexus.crontab"):
        scheduler.add_cron_job("bad-expr", "not a cron", job)
    assert scheduler._cron_jobs["bad-expr"]["next_run_at"] is None
    assert any("bad-expr" in r.message and "永不触发" in r.message for r in caplog.records)


def test_start_self_check_flags_dead_jobs(caplog) -> None:
    import logging

    scheduler = CronScheduler(tick_seconds=1)

    async def job() -> None:
        return None

    scheduler.add_cron_job("dead-job", "99 99 99 99 99", job)
    scheduler._cron_jobs["dead-job"]["next_run_at"] = None
    scheduler._task = None
    with caplog.at_level(logging.ERROR, logger="nexus.crontab"):
        scheduler.start(mode="gated")
    assert any("启动自检" in r.message and "dead-job" in r.message for r in caplog.records)


def test_good_expr_no_error_log(caplog) -> None:
    import logging

    scheduler = CronScheduler(tick_seconds=1)

    async def job() -> None:
        return None

    with caplog.at_level(logging.ERROR, logger="nexus.crontab"):
        scheduler.add_cron_job("good", "30 9 * * *", job)
        scheduler._task = None
        scheduler.start(mode="gated")
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert not errs or all("gate" not in r.message for r in errs), errs


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
