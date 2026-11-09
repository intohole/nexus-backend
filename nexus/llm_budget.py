"""LLM 输出预算：按任务类型解析 token 预算与输出模式。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from nexus.logging import get_logger


class OutputMode(str, Enum):
    DEFAULT = "default"
    CONCISE = "concise"
    JSON = "json"
    PROSE = "prose"


@dataclass(frozen=True)
class TaskBudget:
    max_tokens: int
    temperature: Optional[float] = None
    output_mode: OutputMode = OutputMode.CONCISE
    thinking: Optional[bool] = None
    thinking_budget: Optional[int] = None


PROSE_HINT = (
    "这是创作任务：请输出完整作品，正文充实、有细节、有层次，结尾完整不悬空，"
    "不要截断、不要为节省篇幅而简化内容或草草收尾。"
)

DEFAULT_THINKING_BUDGET = 8192

TASK_BUDGETS: dict[str, TaskBudget] = {
    "chat": TaskBudget(max_tokens=512, output_mode=OutputMode.CONCISE, thinking=False),
    "assistant": TaskBudget(max_tokens=512, output_mode=OutputMode.CONCISE, thinking=False),
    "goodbye": TaskBudget(max_tokens=128, output_mode=OutputMode.CONCISE, thinking=False),
    "extract": TaskBudget(max_tokens=300, temperature=0.2, output_mode=OutputMode.JSON, thinking=False),
    "extraction": TaskBudget(max_tokens=3000, temperature=0.2, output_mode=OutputMode.JSON, thinking=False),
    "classify": TaskBudget(max_tokens=200, temperature=0.2, output_mode=OutputMode.JSON, thinking=False),
    "intent": TaskBudget(max_tokens=200, temperature=0.2, output_mode=OutputMode.JSON, thinking=False),
    "summarize": TaskBudget(max_tokens=800, temperature=0.3, output_mode=OutputMode.CONCISE, thinking=False),
    "analyze": TaskBudget(max_tokens=1200, temperature=0.3, output_mode=OutputMode.DEFAULT, thinking=False),
    "evaluation": TaskBudget(max_tokens=600, temperature=0.2, output_mode=OutputMode.JSON, thinking=False),
    "code": TaskBudget(max_tokens=1500, temperature=0.3, output_mode=OutputMode.DEFAULT, thinking=False),
    "creative": TaskBudget(max_tokens=8000, temperature=0.85, output_mode=OutputMode.PROSE,
                           thinking=True, thinking_budget=8192),
    "writing": TaskBudget(max_tokens=8000, temperature=0.8, output_mode=OutputMode.PROSE,
                          thinking=True, thinking_budget=8192),
    "article": TaskBudget(max_tokens=6000, temperature=0.8, output_mode=OutputMode.PROSE,
                          thinking=True, thinking_budget=8192),
    "report": TaskBudget(max_tokens=4000, temperature=0.3, output_mode=OutputMode.DEFAULT,
                         thinking=True, thinking_budget=4096),
    "research": TaskBudget(max_tokens=2000, temperature=0.3, output_mode=OutputMode.DEFAULT, thinking=False),
    "ppt": TaskBudget(max_tokens=3000, temperature=0.4, output_mode=OutputMode.JSON, thinking=False),
}

def resolve_effective_budget(
    task_type: Optional[str],
    max_tokens: Optional[int],
    temperature: float,
    output_mode: Optional[OutputMode],
    enable_thinking: Optional[bool] = None,
    thinking_budget: Optional[int] = None,
) -> tuple[Optional[int], float, Optional[OutputMode], Optional[bool], Optional[int]]:
    """任务预算与显式参数叠加：显式传参优先，未传参回退任务预算档位。

    thinking 档位语义：机械结构化/小预算/危险邻域档显式 False（GLM 思考吃
    max_tokens，1024~2048 邻域不触发网关 guard 但厂商默认开思考会挤空 content）；
    大预算创作/报告档显式 True+预算封顶（跨 vendor 一致开思考，qwen/volc
    vendor 默认关的释放面）。显式传参恒优先；无档位返回 None 透传网关兜底链。
    """
    if not task_type:
        return max_tokens, temperature, output_mode, enable_thinking, thinking_budget
    budget = TASK_BUDGETS.get(task_type)
    if budget is None:
        return max_tokens, temperature, output_mode, enable_thinking, thinking_budget
    resolved_max = max_tokens if max_tokens is not None else budget.max_tokens
    resolved_temp = temperature if temperature is not None else (budget.temperature or 0.7)
    resolved_mode = output_mode if output_mode is not None else budget.output_mode
    resolved_thinking = enable_thinking if enable_thinking is not None else budget.thinking
    resolved_thinking_budget = (thinking_budget if thinking_budget is not None
                                else budget.thinking_budget)
    return resolved_max, resolved_temp, resolved_mode, resolved_thinking, resolved_thinking_budget


TASK_ALIASES: dict[str, str] = {
    "eval": "evaluation",
}

TASK_NEUTRAL_VALUES: frozenset[str] = frozenset({
    "counter", "binary", "word", "vision", "promo", "generation", "timer",
    "checkin", "simple", "qa", "text", "polish", "embedding", "diet",
    "video_blueprint", "recite", "page", "json_mode", "interview_report",
    "feedback_triage", "feedback_draft", "daily", "complex",
    "character_match", "business_custom", "bugfix",
})

TASK_KNOWN_VALUES: frozenset[str] = (
    frozenset(TASK_BUDGETS) | frozenset(TASK_ALIASES) | TASK_NEUTRAL_VALUES
)

_task_type_warned: set[str] = set()


def canonicalize_task_type(task_type: Optional[str]) -> Optional[str]:
    """近义键归一并告警未登记值：extraction→extract/eval→evaluation，白名单外仅告警不拦截。"""
    logger = get_logger("nexus.llm")
    if not task_type:
        return task_type
    canonical: str = TASK_ALIASES.get(task_type, task_type)
    if canonical not in TASK_KNOWN_VALUES and canonical not in _task_type_warned:
        _task_type_warned.add(canonical)
        logger.warning(
            "unregistered task_type '%s' (no budget tier, split bucket); "
            "register alias or neutral value in llm_budget",
            canonical,
        )
    return canonical


__all__ = [
    "OutputMode",
    "TaskBudget",
    "PROSE_HINT",
    "DEFAULT_THINKING_BUDGET",
    "TASK_BUDGETS",
    "TASK_ALIASES",
    "TASK_NEUTRAL_VALUES",
    "TASK_KNOWN_VALUES",
    "resolve_effective_budget",
    "canonicalize_task_type",
]