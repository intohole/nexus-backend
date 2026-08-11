from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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


PROSE_HINT = (
    "这是创作任务：请输出完整作品，正文充实、有细节、有层次，结尾完整不悬空，"
    "不要截断、不要为节省篇幅而简化内容或草草收尾。"
)

TASK_BUDGETS: dict[str, TaskBudget] = {
    "chat": TaskBudget(max_tokens=512, output_mode=OutputMode.CONCISE),
    "assistant": TaskBudget(max_tokens=512, output_mode=OutputMode.CONCISE),
    "goodbye": TaskBudget(max_tokens=128, output_mode=OutputMode.CONCISE),
    "extract": TaskBudget(max_tokens=300, temperature=0.2, output_mode=OutputMode.JSON),
    "classify": TaskBudget(max_tokens=200, temperature=0.2, output_mode=OutputMode.JSON),
    "intent": TaskBudget(max_tokens=200, temperature=0.2, output_mode=OutputMode.JSON),
    "summarize": TaskBudget(max_tokens=800, temperature=0.3, output_mode=OutputMode.CONCISE),
    "analyze": TaskBudget(max_tokens=1200, temperature=0.3, output_mode=OutputMode.DEFAULT),
    "evaluation": TaskBudget(max_tokens=600, temperature=0.2, output_mode=OutputMode.JSON),
    "code": TaskBudget(max_tokens=1500, temperature=0.3, output_mode=OutputMode.DEFAULT),
    "creative": TaskBudget(max_tokens=8000, temperature=0.85, output_mode=OutputMode.PROSE),
    "writing": TaskBudget(max_tokens=8000, temperature=0.8, output_mode=OutputMode.PROSE),
    "article": TaskBudget(max_tokens=6000, temperature=0.8, output_mode=OutputMode.PROSE),
    "report": TaskBudget(max_tokens=4000, temperature=0.3, output_mode=OutputMode.DEFAULT),
    "research": TaskBudget(max_tokens=2000, temperature=0.3, output_mode=OutputMode.DEFAULT),
}

DEFAULT_TASK_BUDGET = TaskBudget(max_tokens=1500, output_mode=OutputMode.DEFAULT)


def resolve_budget(task_type: Optional[str]) -> TaskBudget:
    if not task_type:
        return DEFAULT_TASK_BUDGET
    return TASK_BUDGETS.get(task_type, DEFAULT_TASK_BUDGET)


__all__ = [
    "OutputMode",
    "TaskBudget",
    "PROSE_HINT",
    "TASK_BUDGETS",
    "DEFAULT_TASK_BUDGET",
    "resolve_budget",
]