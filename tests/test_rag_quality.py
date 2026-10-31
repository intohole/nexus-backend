"""nexus.rag_quality 引用忠实性校验原语守底（suki r7 判例收归 / lyra r14 第二消费方）。"""
from __future__ import annotations

import asyncio

from nexus.rag_quality import (
    extract_citations, judge_support, label_of, repair_citations, run_check,
    strip_citations, validate_citations,
)


def test_extract_citations_default_label():
    assert extract_citations("答案[来源1]与[来源3]再提[来源1]") == [1, 3]
    assert extract_citations("") == []
    assert extract_citations("没有引用的回答") == []


def test_extract_citations_label_param():
    assert extract_citations("见[片段2]", label="片段") == [2]
    assert extract_citations("见[来源2]", label="片段") == []


def test_validate_no_citation():
    out = validate_citations("凭空断言", 3)
    assert out["no_citation"] is True and out["used"] == [] and out["invalid"] == []


def test_validate_ghost_citation():
    out = validate_citations("答[来源5]", 3)
    assert out["invalid"] == [5] and out["used"] == [5] and out["no_citation"] is False


def test_validate_zero_sources_not_flagged():
    out = validate_citations("答[来源1]", 0)
    assert out["no_citation"] is False and out["invalid"] == [1]


def test_label_of_default_and_override():
    assert label_of({}) == "来源"
    assert label_of({"citation_label": "片段"}) == "片段"


def test_run_check_deterministic_only():
    ok = asyncio.run(run_check("答[来源1]", [{"content": "原文", "document_name": "文"}],
                               namespace="T", judge=False))
    assert ok["pass"] is True and "score" not in ok
    ghost = asyncio.run(run_check("答[来源9]", [{"content": "原文"}], namespace="T", judge=False))
    assert ghost["pass"] is False and ghost["invalid"] == [9]
    empty = asyncio.run(run_check("无引用", [{"content": "原文"}], namespace="T", judge=False))
    assert empty["pass"] is False and empty["no_citation"] is True
    no_src = asyncio.run(run_check("无引用也无来源", [], namespace="T", judge=False))
    assert no_src["pass"] is False


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def chat_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.payload


def test_judge_support_scores():
    llm = _FakeLLM({"citations": [
        {"n": 1, "verdict": "supported", "reason": "原文直说"},
        {"n": 2, "verdict": "unsupported", "reason": "张冠李戴"},
    ]})
    out = asyncio.run(judge_support(
        "答[来源1][来源2]",
        [{"content": "原文一", "document_name": "甲"},
         {"content": "原文二", "document_name": "乙"}],
        [1, 2], namespace="T", llm=llm))
    assert out["score"] == 0.5
    assert llm.calls[0]["namespace"] == "T"
    assert "[来源1] (来源: 甲)" in llm.calls[0]["messages"][1]["content"]
    assert llm.calls[0]["max_tokens"] == 800


def test_judge_support_bad_verdict_folds_to_partial():
    llm = _FakeLLM({"citations": [{"n": 1, "verdict": "胡说", "reason": ""}]})
    out = asyncio.run(judge_support("答[来源1]", [{"content": "原文"}], [1],
                                    namespace="T", llm=llm))
    assert out["score"] == 0.5 and out["citations"][0]["verdict"] == "partial"


def test_judge_support_llm_down_returns_empty():
    class _Down:
        async def chat_json(self, *a, **k):
            raise RuntimeError("gateway down")

    out = asyncio.run(judge_support("答[来源1]", [{"content": "原文"}], [1],
                                    namespace="T", llm=_Down()))
    assert out == {}


def test_run_check_pass_matrix_with_judge():
    llm_ok = _FakeLLM({"citations": [{"n": 1, "verdict": "supported", "reason": ""}]})
    ok = asyncio.run(run_check("答[来源1]", [{"content": "原文"}], namespace="T", llm=llm_ok))
    assert ok["pass"] is True and ok["score"] == 1.0
    llm_bad = _FakeLLM({"citations": [{"n": 1, "verdict": "unsupported", "reason": ""}]})
    bad = asyncio.run(run_check("答[来源1]", [{"content": "原文"}], namespace="T", llm=llm_bad))
    assert bad["pass"] is False and bad["score"] == 0.0


def test_strip_citations():
    assert strip_citations("答[来源1]再答[来源2]") == "答再答"
    assert strip_citations("无引用", label="片段") == "无引用"


class _AskLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def ask(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.text


def test_repair_citations_success():
    llm = _AskLLM("平台支持 PDF[来源1]，上限 20MB[来源2]。")
    out = asyncio.run(repair_citations(
        "平台支持 PDF，上限 20MB。",
        [{"content": "支持 PDF", "document_name": "甲"},
         {"content": "上限 20MB", "document_name": "乙"}],
        namespace="T", llm=llm))
    assert out["answer"].startswith("平台支持 PDF")
    assert out["check"]["used"] == [1, 2] and out["check"]["invalid"] == []
    assert out["body_changed"] is False
    assert llm.calls[0]["namespace"] == "T" and llm.calls[0]["temperature"] == 0.0


def test_repair_citations_flags_rewrites():
    llm = _AskLLM("改写过的答案[来源1]。")
    out = asyncio.run(repair_citations("原答案", [{"content": "原文"}], namespace="T", llm=llm))
    assert out["body_changed"] is True
    assert out["check"]["used"] == [1] and out["check"]["invalid"] == []


def test_repair_citations_llm_down_returns_empty():
    class _Down:
        async def ask(self, *a, **k):
            raise RuntimeError("gateway down")

    assert asyncio.run(repair_citations("答", [{"content": "原文"}], namespace="T", llm=_Down())) == {}


def test_repair_citations_empty_sources_short_circuit():
    llm = _AskLLM("不应被调用")
    assert asyncio.run(repair_citations("答", [], namespace="T", llm=llm)) == {}
    assert llm.calls == []
