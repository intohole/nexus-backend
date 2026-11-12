"""DecisionGate 族守底（r208）：状态机迁移守卫+轮次上限+默认值收敛+计划批准协议."""
import pytest

from nexus.clarify import (
    DecisionRoundLimitExceeded,
    PlanDraft,
    PlanStep,
    open_decision,
)


def test_open_decision_marks_default_recommended():
    d = open_decision("d1", "走哪条?", ["A", "B"], default_value="B")
    assert [o.value for o in d.options] == ["A", "B"]
    assert d.options[1].recommended is True
    assert d.default_value == "B"
    assert d.status == "pending"


def test_open_decision_defaults_to_first_option():
    d = open_decision("d2", "走哪条?", ["A", "B"])
    assert d.default_value == "A"
    assert d.options[0].recommended is True


def test_open_decision_rejects_bad_default_and_empty_options():
    with pytest.raises(ValueError):
        open_decision("d3", "?", ["A"], default_value="Z")
    with pytest.raises(ValueError):
        open_decision("d4", "?", ["", "  "])


def test_resolve_single_and_multi_select():
    single = open_decision("d5", "?", ["A", "B"])
    assert single.resolve(["B"]) == ["B"]
    assert single.status == "answered"
    multi = open_decision("d6", "?", ["A", "B", "C"], multi_select=True)
    assert multi.resolve(["A", "C"]) == ["A", "C"]


def test_resolve_guards():
    single = open_decision("d7", "?", ["A", "B"])
    with pytest.raises(ValueError):
        single.resolve([])
    with pytest.raises(ValueError):
        single.resolve(["A", "B"])
    with pytest.raises(ValueError):
        single.resolve(["Z"])
    custom = open_decision("d8", "?", ["A"], allow_custom=True)
    assert custom.resolve(["手写"]) == ["手写"]


def test_accept_default_one_click():
    d = open_decision("d9", "?", ["A", "B"], default_value="B")
    assert d.accept_default() == ["B"]
    assert d.status == "answered"


def test_terminal_state_guards():
    d = open_decision("d10", "?", ["A"])
    d.resolve(["A"])
    with pytest.raises(ValueError):
        d.resolve(["A"])
    c = open_decision("d11", "?", ["A"])
    c.cancel()
    with pytest.raises(ValueError):
        c.accept_default()
    assert c.status == "cancelled"


def test_ttl_expiry_blocks_resolve():
    d = open_decision("d12", "?", ["A", "B"], ttl_seconds=60)
    assert d.is_expired(now=d.created_at + 61) is True
    assert d.is_expired(now=d.created_at + 10) is False
    with pytest.raises(ValueError, match="expired"):
        d.resolve(["A"], now=d.created_at + 61)
    assert d.status == "expired"


def test_next_round_and_round_limit():
    d = open_decision("d13", "?", ["A", "B"], max_rounds=2)
    d.resolve(["A"])
    d.next_round("再确认一次?")
    assert d.round_no == 2 and d.status == "pending" and d.selection == []
    assert "再确认" in d.question
    d.resolve(["B"])
    with pytest.raises(DecisionRoundLimitExceeded):
        d.next_round()


def test_plan_draft_summary_and_approval_decision():
    plan = PlanDraft(
        title="重构登录链",
        steps=[
            PlanStep(title="盘点调用点"),
            PlanStep(title="收归原语", detail="nexus 单源"),
        ],
    )
    assert plan.summary().startswith("重构登录链")
    assert "2. 收归原语: nexus 单源" in plan.summary()
    d = plan.to_decision("plan-1")
    assert [o.value for o in d.options] == ["approve_auto", "approve_step", "revise"]
    assert d.default_value == "approve_auto"
    assert d.options[0].recommended is True
    assert d.context == plan.summary()
    assert d.accept_default() == ["approve_auto"]


def test_payload_protocol_fields():
    d = open_decision("d14", "?", ["A", "B"], ttl_seconds=30, max_rounds=2)
    p = d.payload()
    assert p["id"] == "d14" and p["status"] == "pending"
    assert p["round"] == 1 and p["max_rounds"] == 2
    assert p["ttl_seconds"] == 30
    assert p["options"][0]["value"] == "A"
