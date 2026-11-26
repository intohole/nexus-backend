from __future__ import annotations

from datetime import datetime

from ironman.memory.layers import MemoryItem, MemoryScope

_PRIORITY_IMPORTANCE = {"high": 0.8, "medium": 0.5, "low": 0.2}


def _payload_ok(payload: dict[str, object]) -> bool:
    return not (payload.get("unavailable") or payload.get("success") is False or payload.get("error"))


def payload_items(payload: dict[str, object], scope: MemoryScope) -> list[MemoryItem]:
    if not _payload_ok(payload):
        return []
    raw = payload.get("memories")
    if not isinstance(raw, list):
        return []
    items: list[MemoryItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("memory_content") or "").strip()
        if not content:
            continue
        items.append(
            MemoryItem(
                content=content,
                scope=scope,
                key=str(entry.get("id") or ""),
                importance=_importance(entry),
                created_at=_timestamp(entry.get("created_at")),
                metadata={
                    "source_app": str(entry.get("source_app") or ""),
                    "score": str(entry.get("score") or ""),
                    "priority": str(entry.get("priority") or ""),
                    "superseded": str(bool(entry.get("superseded"))),
                    "revived": str(bool(entry.get("revived"))),
                },
            )
        )
    return items


def _importance(entry: dict[str, object]) -> float:
    score = entry.get("score")
    if isinstance(score, (int, float)):
        return max(0.0, min(1.0, float(score)))
    priority = str(entry.get("priority") or "medium").lower()
    return _PRIORITY_IMPORTANCE.get(priority, 0.5)


def _timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


__all__ = ["payload_items"]
