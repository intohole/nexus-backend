"""nexus 功能旗（ironman/feature_flags.py 同款默认 off 模式）。

r259 B5 记忆接线（contracts.md §6 首接随行形态）：旗关=spawn 系行为与既往逐字节
一致；旗开=spawn prompt 走带记忆装配变体。台账须记「用户点名触发」非触发器自然到点。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_TRUTHY = ("1", "true", "yes")


@dataclass
class FeatureFlags:
    harness_memory_injection: bool = False

    @classmethod
    def from_env(cls) -> "FeatureFlags":
        return cls(
            harness_memory_injection=os.getenv("NEXUS_HARNESS_MEMORY_INJECTION", "").lower() in _TRUTHY,
        )


_flags: FeatureFlags | None = None


def get_feature_flags() -> FeatureFlags:
    global _flags
    if _flags is None:
        _flags = FeatureFlags.from_env()
    return _flags


def reset_feature_flags() -> None:
    global _flags
    _flags = None
