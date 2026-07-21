"""Prompt cache for deterministic (temperature=0) LLM responses.

Only caches when temperature == 0 (deterministic output). Streaming, embedding,
and structured extraction are not cached (no stable output / no text output).
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from cachetools import TTLCache

_DEFAULT_MAXSIZE: int = 256
_DEFAULT_TTL: int = 3600  # 1 hour


class PromptCache:
    """TTL cache keyed on (system, prompt, temperature, max_tokens).

    Key is a sha256 hex digest so cached values are not enumerable by content.
    """

    def __init__(self, maxsize: int = _DEFAULT_MAXSIZE, ttl: int = _DEFAULT_TTL) -> None:
        self._store: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

    @staticmethod
    def make_key(
        system: Optional[str],
        prompt: str,
        temperature: float,
        max_tokens: Optional[int],
    ) -> str:
        raw: str = f"{system or ''}|{prompt}|{temperature}|{max_tokens or 0}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def make_messages_key(
        system: Optional[str],
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
    ) -> str:
        raw: str = f"{system or ''}|{json.dumps(messages, sort_keys=True, ensure_ascii=False)}|{temperature}|{max_tokens or 0}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        if value:
            self._store[key] = value

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


_cache: Optional[PromptCache] = None


def get_prompt_cache() -> PromptCache:
    global _cache
    if _cache is None:
        _cache = PromptCache()
    return _cache
