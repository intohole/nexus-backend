"""RAG 答案引用忠实性校验原语：「答案标明来源」从口号到可测量。

suki r7 citation_service 判例收归（lyra r14 第二消费方达成）。RAG 产品的信任
命门是引用真实性——LLM 会幻觉引用（[来源3] 不存在）、零引用断言、以及引用了
不相关片段的张冠李戴；业务侧直接信任 LLM 的 [来源N] 标注落库 = 承诺零校验。

两层：
- 确定性层（纯函数，零成本）：extract_citations / validate_citations ——
  零引用、幽灵引用（编号越界）在这里就被钉死，不需要 LLM。
- LLM 层 judge_support：逐引用支撑判定（supported/partial/unsupported 三态
  加权得分），一次调用，非推理档；LLM 不可用返回 {}（观测缺失≠校验失败，
  宁缺测量不可造假测量）。

校验是事后异步的——流式答案已吐出不可撤回，价值在测量与缺口信号，不在拦截。
引用形态参数化：lyra 是 [来源N]、suki 是 [片段N]，label 区分；来源条目字段
宽容取值（title/document_name/name 任一在场即用），双仓直替零适配。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from nexus.llm import LLMService, get_llm_service
from nexus.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "extract_citations",
    "validate_citations",
    "judge_support",
    "run_check",
    "repair_citations",
    "strip_citations",
    "citation_pattern",
]


def citation_pattern(label: str = "来源") -> re.Pattern[str]:
    return re.compile(rf"\[{re.escape(label)}(\d+)\]")


def extract_citations(text: str, label: str = "来源") -> list[int]:
    """答案中出现的引用编号（去重升序）。"""
    return sorted({int(m) for m in citation_pattern(label).findall(text or "")})


def validate_citations(answer: str, n_sources: int, label: str = "来源") -> dict[str, Any]:
    """确定性层：零引用 / 幽灵引用（编号越界）。"""
    used = extract_citations(answer, label)
    invalid = [n for n in used if n < 1 or n > n_sources]
    return {
        "used": used,
        "invalid": invalid,
        "no_citation": n_sources > 0 and not used,
    }


JUDGE_PROMPT = (
    "你是引用审查员。给你一段回答和它引用的片段原文。逐个核对：每个 [{label}N] 引用"
    "所支撑的论述，是否真的能从片段 N 的原文中得到支撑？\n"
    "判定标准：supported=原文直接支撑该论述；partial=原文相关但论述有超出原文的引申；"
    "unsupported=原文支撑不了（张冠李戴/编造/引用了无关片段）。\n"
    '只输出 JSON：{{"citations": [{{"n": 1, "verdict": "supported", "reason": "一句话"}}]}}'
)


def _source_title(s: dict[str, Any]) -> str:
    for key in ("title", "document_name", "name", "filename"):
        v = s.get(key)
        if v:
            return str(v)
    return ""


async def judge_support(
    answer: str,
    sources: list[dict[str, Any]],
    citations: list[int],
    *,
    namespace: str,
    model: Optional[str] = None,
    llm: Optional[LLMService] = None,
    max_tokens: int = 800,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """LLM 逐引用支撑判定。返回 {"score": 0~1, "citations": [...]}；LLM 不可用返回 {}。"""
    label = label_of(sources[0] if sources else {})
    cited = []
    for n in citations:
        if 1 <= n <= len(sources):
            s = sources[n - 1] or {}
            cited.append(
                f"[{label}{n}] (来源: {_source_title(s)})\n{(s.get('content') or '')[:800]}"
            )
    if not cited:
        return {}
    llm = llm or get_llm_service()
    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": JUDGE_PROMPT.format(label=label)},
                {"role": "user", "content": "\n\n".join(cited) + "\n\n回答：\n" + answer[:3000]},
            ],
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            namespace=namespace,
        )
    except Exception as e:
        logger.warning("citation judge failed: %s", e)
        return {}
    items = data.get("citations") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return {}
    verdicts = []
    for it in items:
        if not isinstance(it, dict) or "n" not in it:
            continue
        verdict = it.get("verdict", "")
        if verdict not in ("supported", "partial", "unsupported"):
            verdict = "partial"
        verdicts.append({"n": int(it.get("n", 0)), "verdict": verdict,
                         "reason": str(it.get("reason", ""))[:120]})
    if not verdicts:
        return {}
    weight = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0}
    score = sum(weight[v["verdict"]] for v in verdicts) / len(verdicts)
    return {"score": round(score, 2), "citations": verdicts}


def label_of(source: dict[str, Any]) -> str:
    """来源条目携带的引用标签（业务侧在首条来源上标注 label；缺省「来源」）。"""
    label = source.get("citation_label") if isinstance(source, dict) else None
    return str(label) if label else "来源"


def strip_citations(text: str, label: str = "来源") -> str:
    """剥离引用标注后的正文——修复采纳守卫用（修复前后正文必须逐字一致）。"""
    return citation_pattern(label).sub("", text or "").strip()


REPAIR_PROMPT = (
    "你是引用标注员。给你一段回答和它引用的资料片段。逐句核对：每句论述来自哪个片段，"
    "在句末补上 [{label}N] 标注（N=片段编号）。\n"
    "纪律：只补标注，不改动、不增删、不改写任何文字；资料支撑不了的句子保持原样不加标注；"
    "标注编号必须真实对应支撑该句的片段。\n"
    "只输出补完标注后的完整回答，不要任何解释。"
)


async def repair_citations(
    answer: str,
    sources: list[dict[str, Any]],
    *,
    namespace: str,
    model: Optional[str] = None,
    llm: Optional[LLMService] = None,
    max_tokens: int = 2000,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """引用修复重试（retry-loop 原语）：校验失败后带资料重生成一次标注。

    返回 {"answer": 修复文本, "check": 修复文本的确定性复核, "body_changed": 正文是否被改动}；
    LLM 不可用或输出异常返回 {}（失败诚实，不伪装修复成功）。采纳裁决在业务侧：
    建议 check 通过 ∧ body_changed=False 才采纳。
    """
    label = label_of(sources[0] if sources else {})
    corpus = "\n\n".join(
        f"[{label}{i + 1}] (来源: {_source_title(s)})\n{(s.get('content') or '')[:800]}"
        for i, s in enumerate(sources)
    )
    if not corpus.strip():
        return {}
    llm = llm or get_llm_service()
    try:
        repaired = await llm.ask(
            corpus + "\n\n回答：\n" + (answer or "")[:3000],
            system=REPAIR_PROMPT.format(label=label),
            model=model, max_tokens=max_tokens, timeout=timeout, temperature=0.0,
            namespace=namespace,
        )
    except Exception as e:
        logger.warning("citation repair failed: %s", e)
        return {}
    repaired = (repaired or "").strip()
    if not repaired:
        return {}
    return {
        "answer": repaired,
        "check": validate_citations(repaired, len(sources), label),
        "body_changed": strip_citations(repaired, label) != strip_citations(answer, label),
    }


async def run_check(
    answer: str,
    sources: list[dict[str, Any]],
    *,
    namespace: str,
    model: Optional[str] = None,
    llm: Optional[LLMService] = None,
    judge: bool = True,
) -> dict[str, Any]:
    """确定性层 + LLM 层合并入口。pass 判定：有引用 ∧ 无幽灵 ∧ (未判定 ∨ score≥0.5)。

    judge=False 时只跑确定性层（零成本路径，health 巡检/E2E 断言用）。
    """
    label = label_of(sources[0] if sources else {})
    det = validate_citations(answer, len(sources), label)
    result: dict[str, Any] = {
        "used": det["used"], "invalid": det["invalid"], "no_citation": det["no_citation"],
    }
    judged: dict[str, Any] = {}
    if judge and det["used"] and not det["invalid"]:
        judged = await judge_support(answer, sources, det["used"],
                                     namespace=namespace, model=model, llm=llm)
    if judged:
        result["score"] = judged["score"]
        result["citations"] = judged["citations"]
    result["pass"] = (
        bool(det["used"]) and not det["invalid"]
        and (not judged or judged.get("score", 0) >= 0.5)
    )
    return result
