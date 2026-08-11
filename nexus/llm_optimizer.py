from __future__ import annotations

import re
from typing import Optional

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

CONCISENESS_HINT = (
    "回答直击要点，禁止寒暄/过渡句/总结性废话，禁止'好的/当然/综上所述'。"
)

JSON_ONLY_HINT = (
    "严格只输出合法JSON，不要markdown代码块，不要JSON之外的任何文字或解释。"
)

DEFAULT_TOKEN_BUDGET: int = 2500


def estimate_tokens(text: str) -> int:
    """CJK感知的token粗估：中文按字、英文按词、数字/符号按字符。"""
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    words = len(ASCII_WORD_RE.findall(text))
    other = len(text) - cjk - sum(len(m) for m in ASCII_WORD_RE.findall(text))
    return cjk + words + other


def trim_context(text: str, max_tokens: int = DEFAULT_TOKEN_BUDGET) -> str:
    """智能截断：保留头部指令与尾部结论，丢弃中间冗余，超裁自动降级。"""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    budget = max(max_tokens, 64)
    head_budget = budget * 2 // 3
    tail_budget = budget - head_budget
    head = _slice_by_budget(text, head_budget, from_start=True)
    tail = _slice_by_budget(text, tail_budget, from_start=False)
    marker = "\n...[已省略中间N字]...\n"
    return head + marker + tail


def compact_history(
    history: list[dict[str, str]],
    max_turns: int = 6,
    max_tokens: int = DEFAULT_TOKEN_BUDGET,
) -> list[dict[str, str]]:
    """预算内保留最近轮次：先按轮数裁剪，再按token预算裁剪，保证首条为user开头。"""
    if not history:
        return history
    recent = history[-max_turns * 2:]
    kept: list[dict[str, str]] = []
    budget = max(max_tokens, 64)
    for msg in reversed(recent):
        cost = estimate_tokens(str(msg.get("content", "")))
        if sum(estimate_tokens(str(m.get("content", ""))) for m in kept) + cost <= budget:
            kept.append(msg)
        else:
            break
    kept.reverse()
    return kept


def within_budget(text: str, max_tokens: int = DEFAULT_TOKEN_BUDGET) -> bool:
    return estimate_tokens(text) <= max_tokens


def _slice_by_budget(text: str, budget: int, from_start: bool) -> str:
    if budget <= 0:
        return ""
    if from_start:
        return _leading(text, budget)
    return _trailing(text, budget)


def _leading(text: str, budget: int) -> str:
    acc: list[str] = []
    total = 0
    for ch in text:
        cost = 1 if CJK_RE.match(ch) else (1 if ch.isalnum() else 0)
        if cost == 0:
            continue
        if total + cost > budget:
            break
        total += cost
        acc.append(ch)
    return "".join(acc)


def _trailing(text: str, budget: int) -> str:
    acc: list[str] = []
    total = 0
    for ch in reversed(text):
        cost = 1 if CJK_RE.match(ch) else (1 if ch.isalnum() else 0)
        if cost == 0:
            continue
        if total + cost > budget:
            break
        total += cost
        acc.append(ch)
    return "".join(reversed(acc))


__all__ = [
    "CONCISENESS_HINT",
    "JSON_ONLY_HINT",
    "DEFAULT_TOKEN_BUDGET",
    "estimate_tokens",
    "trim_context",
    "compact_history",
    "within_budget",
]