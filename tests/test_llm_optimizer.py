from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexus.llm_optimizer import (
    estimate_tokens,
    trim_context,
    compact_history,
    within_budget,
    CONCISENESS_HINT,
    JSON_ONLY_HINT,
)
from nexus.llm_metrics import LLMMetrics


def test_estimate_tokens_cjk():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello world") == 3
    assert estimate_tokens("abc123") == 1


def test_within_budget():
    assert within_budget("短", 50)
    assert not within_budget("很长" * 100, 10)


def test_trim_context_short_passthrough():
    text = "短文本"
    assert trim_context(text, 1000) == text


def test_trim_context_reduces_tokens():
    text = "这是一个用于验证token估算和截断功能的测试文本。" * 40
    trimmed = trim_context(text, 100)
    assert estimate_tokens(trimmed) <= 120
    assert estimate_tokens(trimmed) < estimate_tokens(text)


def test_trim_context_keeps_head_and_tail():
    text = "开头指令内容" + "中间冗余内容" * 50 + "结尾结论内容"
    trimmed = trim_context(text, 40)
    assert trimmed.startswith("开头指令内容")
    assert trimmed.endswith("结尾结论内容")


def test_compact_history_keeps_recent():
    history = [{"role": "user", "content": f"消息{i}"} for i in range(20)]
    kept = compact_history(history, max_turns=4, max_tokens=100)
    assert len(kept) < len(history)
    assert kept[-1]["content"] == "消息19"


def test_compact_history_pair_keeps_pair():
    ol = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    kept = compact_history(ol, max_turns=1)
    assert kept[0]["role"] == "user"
    assert kept[-1]["role"] == "assistant"


def test_hints_nonempty():
    assert CONCISENESS_HINT.strip()
    assert JSON_ONLY_HINT.strip()


def test_metrics_record_usage_and_cost():
    m = LLMMetrics()
    m.reset()
    m.record("appA", "glm-4-flash", 0.5, tokens=1200, cached=True, cost_usd=0.0012)
    snap = m.snapshot()
    assert snap["total_calls"] == 1
    assert snap["total_tokens"] == 1200
    assert snap["total_cost_usd"] == 0.0012
    assert snap["cached_tokens"] == 1200
    assert snap["by_model"]["glm-4-flash"]["tokens"] == 1200
    assert snap["by_app"]["appA"]["cost_usd"] == 0.0012