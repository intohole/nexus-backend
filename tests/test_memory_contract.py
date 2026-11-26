from __future__ import annotations

import pytest

from nexus.memory_contract import (
    MemoryBudget,
    MemoryOverflowError,
    MemoryUnavailableError,
)


def test_budget_converts_tokens_to_chars():
    # r256 口径：中文主导保守换算 1 字/token
    assert MemoryBudget(max_tokens=100).max_chars == 100
    assert MemoryBudget(max_tokens=0).max_chars == 0


def test_overflow_fail_query_raises_on_truncated():
    contract = MemoryBudget(max_tokens=10, overflow="fail_query")
    with pytest.raises(MemoryOverflowError):
        contract.ensure({"truncated": True})
    MemoryBudget(max_tokens=10).ensure({"truncated": True})


def test_failure_mode_fail_query_raises():
    with pytest.raises(MemoryUnavailableError):
        MemoryBudget(failure_mode="fail_query").on_failure("down")
    MemoryBudget().on_failure("down")
