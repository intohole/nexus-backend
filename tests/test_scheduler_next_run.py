"""scheduler next_run_time=None 防御：None=「暂停」语义弹出回落默认调度，显式 datetime 保留。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from nexus.scheduler import NexusScheduler, sanitize_job_kwargs
from nexus.scheduler_thread import NexusThreadScheduler


class _StubScheduler:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    def add_job(self, *args: object, **kwargs: object) -> None:
        self.captured = kwargs

    def add_listener(self, *args: object, **kwargs: object) -> None:
        return None


class _WarnCapture:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, msg: str) -> None:
        self.messages.append(msg)

    def info(self, msg: str) -> None:
        return None

    def error(self, msg: str, *args: object, **kwargs: object) -> None:
        return None


@pytest.fixture
def stub_async(monkeypatch: pytest.MonkeyPatch) -> _StubScheduler:
    s = _StubScheduler()
    monkeypatch.setattr(NexusScheduler, "_ensure_scheduler", lambda self: s)
    return s


@pytest.fixture
def stub_thread(monkeypatch: pytest.MonkeyPatch) -> _StubScheduler:
    s = _StubScheduler()
    monkeypatch.setattr(NexusThreadScheduler, "_ensure_scheduler", lambda self: s)
    return s


@pytest.fixture
def warn_cap(monkeypatch: pytest.MonkeyPatch) -> _WarnCapture:
    cap = _WarnCapture()
    monkeypatch.setattr("nexus.scheduler.logger", cap)
    return cap


def test_helper_pops_none_keeps_rest() -> None:
    kwargs: dict[str, Any] = {"next_run_time": None, "coalesce": True, "max_instances": 1}
    out = sanitize_job_kwargs(kwargs, "j1")
    assert out == {"coalesce": True, "max_instances": 1}


def test_helper_keeps_explicit_datetime() -> None:
    dt = datetime(2026, 10, 24, 3, 0, 0)
    out = sanitize_job_kwargs({"next_run_time": dt}, "j2")
    assert out["next_run_time"] is dt


def test_async_interval_none_popped(
    stub_async: _StubScheduler, warn_cap: _WarnCapture
) -> None:
    NexusScheduler().add_interval_job(
        lambda: None, "j-async", seconds=60, next_run_time=None, coalesce=True
    )
    assert "next_run_time" not in stub_async.captured
    assert stub_async.captured["coalesce"] is True
    assert any("暂停" in m for m in warn_cap.messages)


def test_thread_interval_none_popped(
    stub_thread: _StubScheduler, warn_cap: _WarnCapture
) -> None:
    NexusThreadScheduler().add_interval_job(
        lambda: None, "j-thread", seconds=1800, next_run_time=None, max_instances=1
    )
    assert "next_run_time" not in stub_thread.captured
    assert stub_thread.captured["max_instances"] == 1


def test_async_interval_explicit_datetime_preserved(
    stub_async: _StubScheduler, warn_cap: _WarnCapture
) -> None:
    dt = datetime.now() + timedelta(seconds=120)
    NexusScheduler().add_interval_job(
        lambda: None, "j-delay", seconds=60, next_run_time=dt
    )
    assert stub_async.captured["next_run_time"] is dt
    assert warn_cap.messages == []


def test_async_cron_none_popped(stub_async: _StubScheduler) -> None:
    NexusScheduler().add_cron_job(
        lambda: None, "j-cron", expr="0 3 * * *", next_run_time=None
    )
    assert "next_run_time" not in stub_async.captured
    assert stub_async.captured["misfire_grace_time"] == 6 * 3600


def test_thread_cron_none_popped(stub_thread: _StubScheduler) -> None:
    NexusThreadScheduler().add_cron_job(
        lambda: None, "j-tcron", hour=3, minute=0, next_run_time=None
    )
    assert "next_run_time" not in stub_thread.captured
