from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OverflowMode = Literal["truncate", "fail_query"]
FailureMode = Literal["best_effort", "fail_query"]
CHARS_PER_TOKEN = 4


class MemoryUnavailableError(RuntimeError):
    pass


class MemoryOverflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryBudget:
    max_tokens: int = 0
    overflow: OverflowMode = "truncate"
    failure_mode: FailureMode = "best_effort"
    chars_per_token: int = CHARS_PER_TOKEN

    @property
    def max_chars(self) -> int:
        if self.max_tokens <= 0:
            return 0
        return self.max_tokens * max(1, self.chars_per_token)

    def ensure(self, readout: dict[str, object]) -> None:
        if self.overflow == "fail_query" and readout.get("truncated"):
            raise MemoryOverflowError(
                f"记忆注入超出预算 {self.max_tokens} tokens"
            )

    def on_failure(self, error: str) -> None:
        if self.failure_mode == "fail_query":
            raise MemoryUnavailableError(error)


__all__ = [
    "CHARS_PER_TOKEN",
    "FailureMode",
    "MemoryBudget",
    "MemoryOverflowError",
    "MemoryUnavailableError",
    "OverflowMode",
]
