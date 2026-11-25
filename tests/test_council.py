from __future__ import annotations

import asyncio

import pytest

from nexus.council import (
    DEGRADE_ASK_FAILED,
    DEGRADE_EMPTY_INVALID_JSON,
    DEGRADE_SEAT_FAILED,
    DEGRADE_TIMEOUT_CAP,
    SeatResult,
    first_error_kind,
    run_seat,
    run_seats,
    seats_ok,
)


async def _ok(value):
    return value


async def _boom(exc: Exception):
    raise exc


class TestRunSeat:
    @pytest.mark.asyncio
    async def test_success_passes_value_through(self):
        seat = await run_seat("attack", lambda: _ok({"issues": [1]}))
        assert seat.ok is True
        assert seat.value == {"issues": [1]}
        assert seat.error_kind is None
        assert seat.error is None

    @pytest.mark.asyncio
    async def test_exception_captured_not_raised(self):
        seat = await run_seat("defend", lambda: _boom(RuntimeError("gateway 502")))
        assert seat.ok is False
        assert seat.value is None
        assert seat.error_kind == "exception"
        assert "RuntimeError" in (seat.error or "")
        assert "gateway 502" in (seat.error or "")

    @pytest.mark.asyncio
    async def test_timeout_mapped_to_timeout_kind(self):
        async def _slow():
            await asyncio.sleep(5)

        seat = await run_seat("judge", _slow, timeout_s=0.01)
        assert seat.ok is False
        assert seat.error_kind == "timeout"

    @pytest.mark.asyncio
    async def test_no_timeout_no_wait_for(self):
        async def _immediate():
            await asyncio.sleep(0.01)
            return "done"

        seat = await run_seat("s", _immediate)
        assert seat.ok is True
        assert seat.value == "done"

    @pytest.mark.asyncio
    async def test_error_summary_truncated(self):
        seat = await run_seat("s", lambda: _boom(RuntimeError("x" * 500)))
        assert len(seat.error) <= 200


class TestRunSeats:
    @pytest.mark.asyncio
    async def test_parallel_failure_isolation(self):
        results = await run_seats({
            "attack": lambda: _ok("a"),
            "defend": lambda: _boom(ValueError("bad json")),
        })
        assert results["attack"].ok is True
        assert results["attack"].value == "a"
        assert results["defend"].ok is False
        assert results["defend"].error_kind == "exception"

    @pytest.mark.asyncio
    async def test_all_seats_run_despite_one_failure(self):
        calls = {f"s{i}": (lambda: _boom(RuntimeError("x"))) if i == 0 else (lambda: _ok(i))
                 for i in range(4)}
        results = await run_seats(calls)
        assert len(results) == 4
        assert seats_ok(results) is False
        assert results["s3"].value == 3

    @pytest.mark.asyncio
    async def test_empty_calls_returns_empty(self):
        assert await run_seats({}) == {}

    @pytest.mark.asyncio
    async def test_all_ok(self):
        results = await run_seats({"a": lambda: _ok(1), "b": lambda: _ok(2)})
        assert seats_ok(results) is True
        assert first_error_kind(results) is None


class TestFailureFace:
    def test_seats_ok_false_on_any_failure(self):
        mixed = {
            "a": SeatResult(name="a", ok=True, value=1),
            "b": SeatResult(name="b", ok=False, error_kind="exception", error="boom"),
        }
        assert seats_ok(mixed) is False

    def test_first_error_kind_timeout_precedence(self):
        mixed = {
            "a": SeatResult(name="a", ok=False, error_kind="exception", error="e"),
            "b": SeatResult(name="b", ok=False, error_kind="timeout", error="t"),
        }
        kind = first_error_kind(mixed)
        assert kind in ("exception", "timeout")

    def test_degrade_constants_stable(self):
        assert DEGRADE_SEAT_FAILED == "seat_failed"
        assert DEGRADE_EMPTY_INVALID_JSON == "empty_invalid_json"
        assert DEGRADE_TIMEOUT_CAP == "timeout_cap"
        assert DEGRADE_ASK_FAILED == "ask_failed"
