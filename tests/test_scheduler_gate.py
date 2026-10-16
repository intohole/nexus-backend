"""scheduler_gate 门禁：auto 向后兼容、gated fail-closed、off 不启动、看门狗不绕过。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from nexus.crontab import CronScheduler
from nexus.scheduler import NexusScheduler
from nexus.scheduler_gate import (
    ENABLED_ENV,
    MANAGED_HINT_ENV,
    MODE_ENV,
    scheduler_gate,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (MODE_ENV, ENABLED_ENV, MANAGED_HINT_ENV):
        monkeypatch.delenv(key, raising=False)


def test_default_auto_allows() -> None:
    decision = scheduler_gate()
    assert decision.allowed and decision.mode == "auto"


def test_gated_without_flag_refuses() -> None:
    decision = scheduler_gate("gated")
    assert not decision.allowed
    assert ENABLED_ENV in decision.reason


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_gated_with_flag_allows(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv(ENABLED_ENV, flag)
    assert scheduler_gate("gated").allowed


def test_gated_rejects_non_truthy_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "0")
    assert not scheduler_gate("gated").allowed


def test_env_mode_applies_when_param_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "gated")
    assert not scheduler_gate().allowed
    monkeypatch.setenv(ENABLED_ENV, "1")
    assert scheduler_gate().allowed


def test_explicit_param_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    assert scheduler_gate("auto").allowed


def test_off_refuses_even_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "1")
    assert not scheduler_gate("off").allowed


def test_unknown_mode_falls_back_to_auto() -> None:
    assert scheduler_gate("whatever").mode == "auto"


def test_managed_hint_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MANAGED_HINT_ENV, "/srv/app")
    assert scheduler_gate("gated").looks_managed


def test_nexus_scheduler_gated_does_not_start(caplog: pytest.LogCaptureFixture) -> None:
    sched = NexusScheduler()

    async def _job() -> None:
        return None

    sched.add_cron_job(_job, job_id="t-gate", hour=3, minute=0)
    with caplog.at_level(logging.WARNING):
        sched.start(mode="gated")
    assert not sched.running
    assert sched.gate is not None and not sched.gate.allowed
    assert "NexusScheduler not started" in caplog.text
    assert sched.gate_reason.startswith("gated:")


def test_nexus_scheduler_gated_logs_error_when_managed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(MANAGED_HINT_ENV, "/srv/app")
    sched = NexusScheduler()
    with caplog.at_level(logging.ERROR):
        sched.start(mode="gated")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_cron_scheduler_gated_and_watchdog_respects_gate() -> None:
    async def _run() -> tuple[bool, bool, bool]:
        sched = CronScheduler(tick_seconds=1)
        sched.start(mode="gated")
        alive_after_start = sched.status()["alive"]
        rebuilt = sched.ensure_alive()
        alive_after_watchdog = sched.status()["alive"]
        await sched.stop()
        return bool(alive_after_start), bool(rebuilt), bool(alive_after_watchdog)

    alive, rebuilt, alive_after = asyncio.run(_run())
    assert not alive and not rebuilt and not alive_after


def test_cron_scheduler_gated_allowed_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "1")

    async def _run() -> tuple[bool, dict[str, object] | None]:
        sched = CronScheduler(tick_seconds=1)
        sched.start(mode="gated")
        status = sched.status()
        await sched.stop()
        return bool(status["alive"]), status["gate"]

    alive, gate = asyncio.run(_run())
    assert alive and gate is not None and gate["allowed"] is True


def test_cron_scheduler_auto_default_unchanged() -> None:
    async def _run() -> bool:
        sched = CronScheduler(tick_seconds=1)
        sched.start()
        alive = sched.status()["alive"]
        await sched.stop()
        return bool(alive)

    assert asyncio.run(_run())
