"""nexus.judge 通用核守底（r57 批2）：跨族选模/钉模语义/断供退避/分维解析。

零真网关——scripted ask/闭包注入（test_harness_loop 同款风格）。
"""

from __future__ import annotations

import asyncio

from nexus.judge import (
    JUDGE_CANDIDATES,
    ask_with_supply_retry,
    model_family,
    parse_group_scores,
    pick_judge_model,
    resolve_judge_model,
)


class CircuitBreakerOpenError(Exception):
    pass


def test_pick_cross_family_and_none_when_same_family():
    assert pick_judge_model() == "glm-4.5-air"
    assert pick_judge_model("glm-4.5-air", candidates=("glm-5-air",)) is None
    assert model_family("DeepSeek-Flash") == "deepseek"


def test_resolve_explicit_pin_skips_candidates():
    picked, model = resolve_judge_model("deepseek-flash")
    assert picked is None and model == "deepseek-flash"
    picked, model = resolve_judge_model("")
    assert picked == "glm-4.5-air" and model == "glm-4.5-air"
    assert JUDGE_CANDIDATES == ("glm-4.5-air",)


def test_supply_retry_recovers_once_then_raises():
    calls = {"n": 0}

    async def flaky() -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 502")
        return {"ok": True}

    out = asyncio.run(ask_with_supply_retry(flaky, retry_delay_s=0))
    assert out == {"ok": True} and calls["n"] == 2

    async def always_dead() -> object:
        raise RuntimeError("dead")

    try:
        asyncio.run(ask_with_supply_retry(always_dead, retry_delay_s=0))
        raise AssertionError("第二次失败必须如实上抛")
    except RuntimeError as exc:
        assert "dead" in str(exc)


def test_supply_retry_no_retry_on_circuit_open():
    calls = {"n": 0}

    async def breaker() -> object:
        calls["n"] += 1
        raise CircuitBreakerOpenError("circuit llm:open")

    try:
        asyncio.run(ask_with_supply_retry(breaker, retry_delay_s=0))
        raise AssertionError("熔断打开必须直接上抛")
    except CircuitBreakerOpenError:
        assert calls["n"] == 1


def test_parse_group_scores_bounds_and_missing_keys():
    dims = ("playability", "clarity")
    assert parse_group_scores({"playability": 2, "clarity": 1, "feedback": ["好", 5]}, dims) == (
        {"playability": 2, "clarity": 1}, ["好"])
    assert parse_group_scores({"playability": 3, "clarity": 1}, dims) is None
    assert parse_group_scores({"playability": 2}, dims) is None
    assert parse_group_scores(["not", "dict"], dims) is None
    assert parse_group_scores({"playability": 2, "clarity": 5}, dims, cap=5)[0]["clarity"] == 5
