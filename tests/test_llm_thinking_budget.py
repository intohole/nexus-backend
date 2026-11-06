"""thinking 档位守底：任务语义→思考策略解析叠加（r73）——撤档位/撤叠加必红。"""
from __future__ import annotations

import pytest

from nexus.llm import LLMService
from nexus.llm_budget import DEFAULT_THINKING_BUDGET, OutputMode, resolve_effective_budget


def test_thinking_on_tiers_resolve_true_with_budget_cap():
    for task, budget in (("creative", 8192), ("writing", 8192), ("article", 8192), ("report", 4096)):
        *_, thinking, thinking_budget = resolve_effective_budget(task, None, 0.7, None)
        assert thinking is True, task
        assert thinking_budget == budget, task


def test_thinking_off_tiers_resolve_false():
    for task in ("chat", "assistant", "goodbye", "extract", "classify", "intent",
                 "summarize", "analyze", "evaluation", "code", "research", "ppt"):
        *_, thinking, thinking_budget = resolve_effective_budget(task, None, 0.7, None)
        assert thinking is False, task
        assert thinking_budget is None, task


def test_explicit_thinking_wins_over_tier():
    *_, thinking, _ = resolve_effective_budget("extract", None, 0.7, None, enable_thinking=True)
    assert thinking is True
    *_, thinking, _ = resolve_effective_budget("creative", None, 0.7, None, enable_thinking=False)
    assert thinking is False


def test_explicit_thinking_budget_wins_over_tier():
    *_, _, thinking_budget = resolve_effective_budget("report", None, 0.7, None, thinking_budget=2048)
    assert thinking_budget == 2048
    *_, thinking, thinking_budget = resolve_effective_budget(
        "creative", None, 0.7, None, enable_thinking=True, thinking_budget=16384)
    assert thinking is True
    assert thinking_budget == 16384


def test_no_task_or_unknown_task_passthrough_thinking():
    assert resolve_effective_budget(None, None, 0.7, None)[3:] == (None, None)
    *_, thinking, thinking_budget = resolve_effective_budget("qa", None, 0.7, None)
    assert thinking is None
    assert thinking_budget is None


def test_thinking_on_tier_keeps_budget_fields_unchanged():
    max_t, temp, mode, _, _ = resolve_effective_budget("creative", None, None, None)
    assert max_t == 8000
    assert temp == 0.85
    assert mode == OutputMode.PROSE


def _fake_configure(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr("nexus.llm_core.configure_ironman", _noop)


@pytest.mark.asyncio
async def test_prepare_call_resolves_tier_thinking(monkeypatch):
    _fake_configure(monkeypatch)
    core = LLMService()
    opts, _, _, _ = await core._prepare_call(
        temperature=0.7, max_tokens=None, task_type="creative", output_mode=None,
        json_mode=False, namespace=None, model=None,
        enable_thinking=None, thinking_budget=None,
    )
    assert opts.enable_thinking is True
    assert opts.thinking_budget == 8192


@pytest.mark.asyncio
async def test_prepare_call_off_tier_collapses_false(monkeypatch):
    _fake_configure(monkeypatch)
    core = LLMService()
    opts, _, _, _ = await core._prepare_call(
        temperature=0.7, max_tokens=None, task_type="extract", output_mode=None,
        json_mode=False, namespace=None, model=None,
        enable_thinking=None, thinking_budget=None,
    )
    assert opts.enable_thinking is False


@pytest.mark.asyncio
async def test_prepare_call_explicit_overrides_tier(monkeypatch):
    _fake_configure(monkeypatch)
    core = LLMService()
    opts, _, _, _ = await core._prepare_call(
        temperature=0.7, max_tokens=None, task_type="extract", output_mode=None,
        json_mode=False, namespace=None, model=None,
        enable_thinking=True, thinking_budget=None,
    )
    assert opts.enable_thinking is True
    assert opts.thinking_budget == DEFAULT_THINKING_BUDGET
