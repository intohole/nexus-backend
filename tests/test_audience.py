"""nexus.audience 受众净化原语测试（challengePlanet reminder_guard 判例收归对齐）。"""
from __future__ import annotations

import asyncio

import pytest

from nexus import audience


def _reset_cache() -> None:
    audience._status_cache.clear()


def test_sendable_users_fail_open_on_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(ids):
        return None

    monkeypatch.setattr(audience, "_fetch_statuses", boom)
    out = asyncio.run(audience.sendable_users({"1001", "2002"}))
    assert out == {"1001", "2002"}
    _reset_cache()


def test_sendable_users_filters_orphan_and_test(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(ids):
        return {
            "1001": {"exists": True, "is_test": False, "disabled": False},
            "2002": {"exists": True, "is_test": True, "disabled": False},
            "3003": {"exists": False, "is_test": False, "disabled": False},
        }

    monkeypatch.setattr(audience, "_fetch_statuses", fake)
    out = asyncio.run(audience.sendable_users({"1001", "2002", "3003"}))
    assert out == {"1001"}
    assert audience._status_cache["2002"][1] is False
    _reset_cache()


def test_sendable_users_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMINDER_GUARD_OFF", "1")
    out = asyncio.run(audience.sendable_users({"1001"}))
    assert out == {"1001"}
    monkeypatch.delenv("REMINDER_GUARD_OFF")


def test_sendable_users_cache_hit_skips_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake(ids):
        calls.append(list(ids))
        return {"1001": {"exists": True, "is_test": False, "disabled": False}}

    monkeypatch.setattr(audience, "_fetch_statuses", fake)
    first = asyncio.run(audience.sendable_users({"1001"}))
    second = asyncio.run(audience.sendable_users({"1001"}))
    assert first == second == {"1001"}
    assert len(calls) == 1
    _reset_cache()


def test_sendable_users_empty_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(ids):
        return {}

    monkeypatch.setattr(audience, "_fetch_statuses", fake)
    out = asyncio.run(audience.sendable_users({"1001"}))
    assert out == set()
    _reset_cache()


def test_sendable_single(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(ids):
        return {"7": {"exists": True, "is_test": False, "disabled": True}}

    monkeypatch.setattr(audience, "_fetch_statuses", fake)
    assert asyncio.run(audience.sendable("7")) is False
    _reset_cache()
