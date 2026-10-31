"""S82 prompt 寄存补刀守底：rag_quality 双模板渲染等价 + call_sites 防回潮。"""
from __future__ import annotations

from pathlib import Path

from nexus.rag_quality import JUDGE_PROMPT, REPAIR_PROMPT, _citation_prompt

_SOURCE = Path(__file__).resolve().parents[1] / "nexus" / "rag_quality.py"
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

_ORIG_JUDGE = (
    "你是引用审查员。给你一段回答和它引用的片段原文。逐个核对：每个 [{label}N] 引用"
    "所支撑的论述，是否真的能从片段 N 的原文中得到支撑？\n"
    "判定标准：supported=原文直接支撑该论述；partial=原文相关但论述有超出原文的引申；"
    "unsupported=原文支撑不了（张冠李戴/编造/引用了无关片段）。\n"
    '只输出 JSON：{{"citations": [{{"n": 1, "verdict": "supported", "reason": "一句话"}}]}}'
)
_ORIG_REPAIR = (
    "你是引用标注员。给你一段回答和它引用的资料片段。逐句核对：每句论述来自哪个片段，"
    "在句末补上 [{label}N] 标注（N=片段编号）。\n"
    "纪律：只补标注，不改动、不增删、不改写任何文字；资料支撑不了的句子保持原样不加标注；"
    "标注编号必须真实对应支撑该句的片段。\n"
    "只输出补完标注后的完整回答，不要任何解释。"
)


def test_judge_prompt_equivalence() -> None:
    for label in ("S", "来源", "片段"):
        assert _citation_prompt(JUDGE_PROMPT, label) == _ORIG_JUDGE.format(label=label)


def test_repair_prompt_equivalence() -> None:
    for label in ("S", "来源", "片段"):
        assert _citation_prompt(REPAIR_PROMPT, label) == _ORIG_REPAIR.format(label=label)


def test_prompt_templates_on_disk() -> None:
    assert (_PROMPTS_DIR / "rag_citation_judge.md").is_file()
    assert (_PROMPTS_DIR / "rag_citation_repair.md").is_file()


def test_call_sites_no_inline_regression() -> None:
    text = _SOURCE.read_text(encoding="utf-8")
    assert '_citation_prompt(JUDGE_PROMPT, label)' in text
    assert '_citation_prompt(REPAIR_PROMPT, label)' in text
    assert "你是引用审查员" not in text, "judge prompt 内联回潮"
    assert "你是引用标注员" not in text, "repair prompt 内联回潮"
