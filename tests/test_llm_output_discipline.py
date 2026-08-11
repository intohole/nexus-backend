from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexus.llm import DEFAULT_MAX_OUTPUT_TOKENS, _apply_output_discipline
from nexus.llm_optimizer import CONCISENESS_HINT, JSON_ONLY_HINT


def test_default_cap_is_positive():
    assert DEFAULT_MAX_OUTPUT_TOKENS > 0


def test_discipline_passthrough_when_not_concise():
    system = "你是助手"
    prompt = "你好"
    s, p = _apply_output_discipline(system, prompt, concise=False, json_mode=False)
    assert s == system
    assert p == prompt


def test_discipline_concise_text_appends_hint():
    s, p = _apply_output_discipline("你是助手", "你好", concise=True, json_mode=False)
    assert CONCISENESS_HINT in s
    assert s.startswith("你是助手")
    assert p == "你好"


def test_discipline_concise_no_system_returns_hint():
    s, p = _apply_output_discipline(None, "你好", concise=True, json_mode=False)
    assert s == CONCISENESS_HINT
    assert p == "你好"


def test_discipline_json_appends_json_hint():
    s, p = _apply_output_discipline("输出数据", "给数据", concise=True, json_mode=True)
    assert JSON_ONLY_HINT in s
    assert "输出数据" in s