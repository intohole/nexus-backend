"""视觉任务档位：把业务语义 profile 映射为 detail/尺寸/token 预算。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

VisionProfile = Literal["auto", "ocr", "chart", "ui", "photo", "quick"]


@dataclass(frozen=True)
class VisionPlan:
    detail: str
    max_side: int
    target_tokens: int


_PLANS: dict[str, VisionPlan] = {
    "auto": VisionPlan(detail="auto", max_side=2048, target_tokens=1600),
    "ocr": VisionPlan(detail="high", max_side=2560, target_tokens=2500),
    "chart": VisionPlan(detail="high", max_side=2048, target_tokens=2000),
    "ui": VisionPlan(detail="auto", max_side=2048, target_tokens=1800),
    "photo": VisionPlan(detail="auto", max_side=1568, target_tokens=1200),
    "quick": VisionPlan(detail="low", max_side=1024, target_tokens=400),
}

_ALIASES: dict[str, str] = {
    "": "auto",
    "default": "auto",
    "image": "auto",
    "text": "ocr",
    "document": "ocr",
    "doc": "ocr",
    "table": "ocr",
    "screenshot": "ui",
    "screen": "ui",
    "web": "ui",
    "diagram": "chart",
    "graph": "chart",
    "avatar": "quick",
    "thumbnail": "quick",
}


def resolve_profile(profile: str | None) -> str:
    key = str(profile or "").strip().lower()
    key = _ALIASES.get(key, key)
    return key if key in _PLANS else "auto"


def plan_for(profile: str | None) -> VisionPlan:
    return _PLANS[resolve_profile(profile)]


def detail_for(profile: str | None, explicit: str | None = None) -> str:
    if explicit:
        return str(explicit).strip().lower()
    return plan_for(profile).detail


__all__ = [
    "VisionProfile",
    "VisionPlan",
    "detail_for",
    "plan_for",
    "resolve_profile",
]
