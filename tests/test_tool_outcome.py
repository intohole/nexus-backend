"""工具结果三态原语单测（dsh v0.2.0-rc.1 语义收归，harness 调研 A1/backlog#11）。"""
from nexus.tool_outcome import tool_outcome_kind, unknown_outcome_hint


def test_outcome_kind_three_states():
    assert tool_outcome_kind(True, {"error": "timeout"}) == "unknown"
    assert tool_outcome_kind(False, {"error": "bad request"}) == "known-failed"
    assert tool_outcome_kind(False, {"code": 200, "data": {"id": 1}}) == "success"
    assert tool_outcome_kind(False, "plain text") == "success"
    assert tool_outcome_kind(False, None) == "success"


def test_unknown_hint_carries_verification_semantics():
    hint = unknown_outcome_hint("create_task", 30.5)
    assert "create_task" in hint and "30" in hint
    assert "请勿重复调用" in hint
    assert "核实" in hint
    assert "严禁声称已成功" in hint
    assert unknown_outcome_hint("x", None).startswith("能力 x 执行超时")
