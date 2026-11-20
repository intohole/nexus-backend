"""S176 AM4 注入预算测试：render_memory_context 分层截断+注入量读数。

Anthropic context editing 形态的预算等价物：超限截断带尾标，
读数（n_items/n_chars/truncated）供上游观测，老签名零破坏。
"""
import pytest

from nexus.memory import render_memory_context, render_memory_context_bounded

ENVELOPE = {
    "memories": [
        {"id": "m1", "content": "用户对花生严重过敏，随身携带肾上腺素笔", "source_app": "aipet", "created_at": "2026-10-28T09:30:00"},
        {"id": "m2", "content": "用户在规划北海道旅行，准备冬季出行装备清单", "source_app": "nexus-agent", "created_at": "2026-10-01T12:00:00"},
        {"id": "m3", "content": "用户偏好简洁回答与要点列表", "source_app": "", "created_at": "2026-09-15T08:00:00"},
    ],
    "count": 3,
}


def test_bounded_full_within_budget():
    text, readout = render_memory_context_bounded(ENVELOPE, budget_chars=5000)
    assert "花生" in text and "北海道" in text and "简洁" in text
    assert readout["n_items"] == 3
    assert readout["truncated"] is False
    assert readout["n_chars"] == len(text)


def test_bounded_truncates_with_marker():
    text, readout = render_memory_context_bounded(ENVELOPE, budget_chars=80)
    assert readout["truncated"] is True
    assert readout["n_items"] < 3
    assert "另有" in text and "条记忆因预算截断" in text
    assert len(text) <= 80 + 60


def test_bounded_zero_budget_keeps_header_marker_only():
    text, readout = render_memory_context_bounded(ENVELOPE, budget_chars=1)
    assert readout["n_items"] == 0
    assert readout["truncated"] is True


def test_bounded_none_budget_equals_legacy():
    legacy = render_memory_context(ENVELOPE)
    bounded, readout = render_memory_context_bounded(ENVELOPE, budget_chars=None)
    assert bounded == legacy
    assert readout["truncated"] is False


def test_legacy_signature_unchanged():
    out = render_memory_context(ENVELOPE)
    assert isinstance(out, str) and "花生" in out


def test_bounded_empty_envelope():
    text, readout = render_memory_context_bounded({"memories": []}, budget_chars=100)
    assert text == ""
    assert readout["n_items"] == 0 and readout["truncated"] is False
