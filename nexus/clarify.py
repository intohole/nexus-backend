"""通用意图澄清协议: 供各应用在 AI 需要用户明确关键信息时复用.

统一数据模型与纯静态工具, 支持多选/单选/自定义输入/可跳过, 以及
"静态候选 + LLM 补全 + 推荐标记" 的选项装配方式. 应用仅需提供问题
定义与候选来源, 无需各自维护一套澄清实现.
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

SKIP_WORDS = ("跳过", "直接生成", "快速生成", "都可以", "随便", "你决定", "就这样", "快速")


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

    def option_values(self) -> List[str]:
        return [opt.value for opt in self.options]


def is_skip(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(word in text for word in SKIP_WORDS)


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


def _normalize(value: object) -> Optional[str]:
    if isinstance(value, list):
        text = "；".join(str(v).strip() for v in value if str(v).strip())
    else:
        text = str(value or "").strip()
    if not text or is_skip(text):
        return None
    return text