from __future__ import annotations

import json
import re
from typing import Any, Optional

from nexus.llm import LLMService, get_llm_service
from nexus.llm_utils import strip_code_fence
from nexus.logging import get_logger
from nexus.web_search import get_web_search_service

logger = get_logger("nexus.deep_research")


def _parse_json_flexible(raw: str) -> Any:
    text = strip_code_fence(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


class DeepResearchService:
    _instance: Optional["DeepResearchService"] = None

    def __new__(cls) -> "DeepResearchService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def research(
        self,
        query: str,
        max_loops: int = 3,
        max_results: int = 5,
        max_tokens_per_round: int = 800,
        summary_max_tokens: int = 2000,
    ) -> dict[str, Any]:
        if not query or not query.strip():
            return {"topic": query, "success": False, "error": "查询不能为空", "findings": [], "final_report": ""}

        llm = get_llm_service()
        search = get_web_search_service()
        all_findings: list[dict[str, Any]] = []

        for loop in range(max_loops):
            try:
                results = await search.search(f"{query} 第{loop + 1}轮深挖", count=max_results)
                if not results:
                    logger.info("Deep research loop %d: no results, stopping", loop + 1)
                    break
                search_text = "\n".join(
                    f"- {r.get('title', '')}: {str(r.get('content', ''))[:150]}"
                    for r in results[:5]
                    if r.get("title")
                )
                prompt = (
                    f"研究主题: {query}\n"
                    f"第{loop + 1}轮搜索结果:\n{search_text}\n"
                    f"已有发现: {json.dumps(all_findings, ensure_ascii=False)}\n"
                    f"请提炼本轮新发现(输出JSON数组,每项含finding/evidence/implication字段):"
                )
                raw = await llm.ask(prompt, temperature=0.2, max_tokens=max_tokens_per_round)
                data = _parse_json_flexible(raw)
                if isinstance(data, list):
                    all_findings.extend(data)
                elif isinstance(data, dict) and "findings" in data:
                    findings = data["findings"]
                    if isinstance(findings, list):
                        all_findings.extend(findings)
            except Exception as e:
                logger.warning("Deep research loop %d failed: %s", loop + 1, e)

        try:
            summary_prompt = (
                f"基于{max_loops}轮研究，主题: {query}\n"
                f"发现列表: {json.dumps(all_findings, ensure_ascii=False)}\n"
                f"请生成800-1500字深度研究报告，包含: 背景、现状、关键发现、投资机会、风险。"
            )
            final_report = await llm.ask(summary_prompt, temperature=0.3, max_tokens=summary_max_tokens)
        except Exception as e:
            logger.error("Deep research summary failed: %s", e)
            return {
                "topic": query,
                "success": False,
                "error": str(e),
                "findings": all_findings,
                "final_report": f"深度研究摘要生成失败: {e}",
            }

        return {
            "topic": query,
            "success": True,
            "error": "",
            "findings": all_findings,
            "final_report": final_report,
        }


_deep_research_service: Optional[DeepResearchService] = None


def get_deep_research_service() -> DeepResearchService:
    global _deep_research_service
    if _deep_research_service is None:
        _deep_research_service = DeepResearchService()
    return _deep_research_service
