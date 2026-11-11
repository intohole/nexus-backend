"""task_type 值域归一守底：别名映射/白名单/未登记降级三断言族（S105 C1/C2）.

归一层单点=llm_budget.canonicalize_task_type；挂点=llm_core._prepare_call +
llm_probes.extract。近义键 extraction/eval 在网关侧归并，业务仓调用点零触碰。
"""
import logging

from nexus.llm_budget import (
    OutputMode,
    TASK_ALIASES,
    TASK_BUDGETS,
    TASK_KNOWN_VALUES,
    TASK_NEUTRAL_VALUES,
    canonicalize_task_type,
    resolve_effective_budget,
)


def test_alias_values_canonicalize() -> None:
    assert canonicalize_task_type("eval") == "evaluation"
    assert canonicalize_task_type("rerank") == "extraction"
    assert canonicalize_task_type("eval_batch") == "evaluation"


def test_extraction_is_own_tier_not_alias() -> None:
    """r92 翻案：pm 生产 14 天实证 extraction 输出 p99=2327，extract 档 300 帽
    不适配——升格独立档位键（预算语义分叉），S105 的别名归一撤除。"""
    assert canonicalize_task_type("extraction") == "extraction"


def test_canonical_values_pass_through_idempotent() -> None:
    assert canonicalize_task_type("extract") == "extract"
    assert canonicalize_task_type("evaluation") == "evaluation"
    assert canonicalize_task_type("counter") == "counter"


def test_empty_values_pass_through() -> None:
    assert canonicalize_task_type(None) is None
    assert canonicalize_task_type("") == ""


def test_unregistered_value_passes_through_with_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        assert canonicalize_task_type("brand_new_task") == "brand_new_task"
    assert any("brand_new_task" in r.message for r in caplog.records)


def test_extraction_tier_direct_hit() -> None:
    """extraction 独立档直接命中（3000/0.2/JSON），无需经别名。"""
    max_tokens, temp, mode, _, _ = resolve_effective_budget(
        canonicalize_task_type("extraction"), None, 0.7, None
    )
    assert max_tokens == 3000
    assert temp == 0.7
    assert mode == OutputMode.JSON


def test_alias_unblocks_budget_tier() -> None:
    max_tokens2, _, _, _, _ = resolve_effective_budget(
        canonicalize_task_type("eval"), None, 0.7, None
    )
    assert max_tokens2 == 600


def test_alias_raw_value_misses_budget_tier() -> None:
    max_tokens, _, mode, _, _ = resolve_effective_budget("brand_new_task", None, 0.7, None)
    assert max_tokens is None
    assert mode is None


def test_known_values_superset_invariants() -> None:
    assert TASK_KNOWN_VALUES >= frozenset(TASK_BUDGETS)
    assert TASK_KNOWN_VALUES >= frozenset(TASK_ALIASES)
    assert TASK_KNOWN_VALUES >= TASK_NEUTRAL_VALUES
    for alias, target in TASK_ALIASES.items():
        assert target in TASK_BUDGETS, f"alias {alias} 指向无档键 {target}"
        assert alias not in TASK_BUDGETS, f"别名 {alias} 不应同时是 canonical 键"


def test_neutral_values_have_no_budget_tier() -> None:
    for value in TASK_NEUTRAL_VALUES:
        assert value not in TASK_BUDGETS
