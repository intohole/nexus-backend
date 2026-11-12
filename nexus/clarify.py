"""通用意图澄清协议: 供各应用在 AI 需要用户明确关键信息时复用.

统一数据模型与纯静态工具, 支持多选/单选/自定义输入/可跳过, 以及
"静态候选 + LLM 补全 + 推荐标记" 的选项装配方式. 应用仅需提供问题
定义与候选来源, 无需各自维护一套澄清实现.

DecisionGate 族（r208 起）: 会话级待决状态机与计划批准协议.
DecisionSession 承载"默认值 + 一次点击 + 轮次上限 + 过期"的收敛语义,
PlanDraft 把多步计划装成三选项批准（自动执行/逐项批准/继续修改）.
原语只管协议与状态迁移, 前端透传与接线归各应用.
"""
from __future__ import annotations

import time
from typing import List, Optional

from pydantic import BaseModel, Field


class ClarifyOption(BaseModel):
    value: str
    label: Optional[str] = None
    recommended: bool = False

    def payload(self) -> dict:
        return {"value": self.value, "label": self.label or self.value, "recommended": self.recommended}


class ClarifyQuestion(BaseModel):
    key: str
    question: str
    type: str = "single"
    options: List[ClarifyOption] = Field(default_factory=list)
    allow_custom: bool = True
    required: bool = True
    max_select: Optional[int] = None
    placeholder: Optional[str] = None
    recommended: bool = False

    def payload(self) -> dict:
        data = {
            "key": self.key,
            "question": self.question,
            "type": self.type,
            "options": [opt.payload() for opt in self.options],
            "allow_custom": self.allow_custom,
            "required": self.required,
            "recommended": self.recommended,
        }
        if self.max_select is not None:
            data["max_select"] = self.max_select
        if self.placeholder:
            data["placeholder"] = self.placeholder
        return data


def question(
    key: str,
    text: str,
    static: List[str],
    llm: Optional[List[str]] = None,
    allow_custom: bool = True,
) -> ClarifyQuestion:
    merged = _merge_candidates(static, llm)
    return ClarifyQuestion(
        key=key,
        question=text,
        options=merged,
        allow_custom=allow_custom,
        recommended=bool(llm),
    )


def _merge_candidates(
    static: List[str],
    llm: Optional[List[str]],
) -> List[ClarifyOption]:
    ordered: List[str] = []
    if llm:
        for value in llm:
            value = value.strip()
            if value and value not in ordered:
                ordered.append(value)
    for value in static:
        value = value.strip()
        if value and value not in ordered:
            ordered.append(value)
    return [
        ClarifyOption(value=value, recommended=(bool(llm) and i == 0))
        for i, value in enumerate(ordered)
    ]


class DecisionRoundLimitExceeded(Exception):
    """确认轮次超限: 防确认地狱, 超限后应按默认值收敛而非继续追问."""


_DECISION_TERMINAL = {"answered", "expired", "cancelled"}


class DecisionSession(BaseModel):
    id: str
    question: str
    context: Optional[str] = None
    options: List[ClarifyOption] = Field(default_factory=list)
    default_value: Optional[str] = None
    multi_select: bool = False
    allow_custom: bool = False
    max_rounds: int = 1
    round_no: int = 1
    ttl_seconds: Optional[int] = None
    status: str = "pending"
    selection: List[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    answered_at: Optional[float] = None

    def payload(self) -> dict:
        data = {
            "id": self.id,
            "question": self.question,
            "context": self.context,
            "options": [opt.payload() for opt in self.options],
            "default_value": self.default_value,
            "multi_select": self.multi_select,
            "allow_custom": self.allow_custom,
            "round": self.round_no,
            "max_rounds": self.max_rounds,
            "status": self.status,
        }
        if self.ttl_seconds is not None:
            data["ttl_seconds"] = self.ttl_seconds
        return data

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.ttl_seconds is None:
            return False
        if self.status in _DECISION_TERMINAL:
            return False
        current = time.time() if now is None else now
        return current - self.created_at > self.ttl_seconds

    def _ensure_pending(self, now: Optional[float] = None) -> None:
        if self.status in _DECISION_TERMINAL:
            raise ValueError(f"decision {self.id} already {self.status}")
        if self.is_expired(now):
            self.status = "expired"
            raise ValueError(f"decision {self.id} expired")

    def resolve(self, selection: List[str], now: Optional[float] = None) -> List[str]:
        self._ensure_pending(now)
        if not selection:
            raise ValueError("selection is empty; use cancel() instead")
        if not self.multi_select and len(selection) > 1:
            raise ValueError("single-select decision got multiple selections")
        known = {opt.value for opt in self.options}
        if not self.allow_custom:
            unknown = [v for v in selection if v not in known]
            if unknown:
                raise ValueError(f"unknown selections: {unknown}")
        self.selection = list(selection)
        self.status = "answered"
        self.answered_at = time.time()
        return self.selection

    def accept_default(self, now: Optional[float] = None) -> List[str]:
        self._ensure_pending(now)
        if self.default_value is None:
            raise ValueError(f"decision {self.id} has no default_value")
        return self.resolve([self.default_value], now=now)

    def cancel(self) -> None:
        self._ensure_pending()
        self.status = "cancelled"

    def next_round(self, question: Optional[str] = None) -> "DecisionSession":
        if self.status in ("cancelled", "expired"):
            raise ValueError(f"decision {self.id} is {self.status}; cannot reopen")
        if self.round_no >= self.max_rounds:
            raise DecisionRoundLimitExceeded(
                f"decision {self.id} hit max_rounds={self.max_rounds}; "
                "收敛到默认值而非继续追问"
            )
        self.round_no += 1
        self.status = "pending"
        self.selection = []
        self.answered_at = None
        if question:
            self.question = question
        return self


def open_decision(
    id: str,
    question: str,
    options: List[str],
    default_value: Optional[str] = None,
    context: Optional[str] = None,
    multi_select: bool = False,
    allow_custom: bool = False,
    max_rounds: int = 1,
    ttl_seconds: Optional[int] = None,
) -> DecisionSession:
    cleaned = [v.strip() for v in options if v and v.strip()]
    if not cleaned:
        raise ValueError("decision needs at least one option")
    if default_value is not None and default_value not in cleaned:
        raise ValueError(f"default_value {default_value!r} not in options")
    if default_value is None:
        default_value = cleaned[0]
    recommended = cleaned.index(default_value)
    return DecisionSession(
        id=id,
        question=question,
        context=context,
        options=[
            ClarifyOption(value=v, recommended=(i == recommended))
            for i, v in enumerate(cleaned)
        ],
        default_value=default_value,
        multi_select=multi_select,
        allow_custom=allow_custom,
        max_rounds=max_rounds,
        ttl_seconds=ttl_seconds,
    )


class PlanStep(BaseModel):
    title: str
    detail: Optional[str] = None


class PlanDraft(BaseModel):
    title: str
    steps: List[PlanStep] = Field(default_factory=list)

    def summary(self) -> str:
        lines = [self.title]
        for i, step in enumerate(self.steps, 1):
            line = f"{i}. {step.title}"
            if step.detail:
                line += f": {step.detail}"
            lines.append(line)
        return "\n".join(lines)

    def to_decision(
        self,
        decision_id: str,
        max_rounds: int = 2,
        ttl_seconds: Optional[int] = None,
    ) -> DecisionSession:
        return open_decision(
            id=decision_id,
            question=f"计划「{self.title}」是否按此执行?",
            options=["approve_auto", "approve_step", "revise"],
            default_value="approve_auto",
            context=self.summary(),
            max_rounds=max_rounds,
            ttl_seconds=ttl_seconds,
        )


