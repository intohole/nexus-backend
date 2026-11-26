from __future__ import annotations

GUEST_IDS = ("", "guest", "anonymous")

RECALL_HEADER = "以下是用户的历史记忆（命中 {count} 条），帮助更贴合地回答："
SNAPSHOT_HEADER = "以下是用户的长期记忆画像（最相关 {count} 条）："
FOOTER = "使用指引：以上记忆仅供理解用户背景，不要逐条复述；与用户本轮表述冲突时，以本轮表述为准。"
SUPERSEDED_NOTE = "已被用户后来的表述更新"
REVIVED_NOTE = "重新启用的较早记忆"


def as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def as_date(value: object) -> str:
    text = as_str(value)
    return text[:10] if len(text) >= 10 else ""


def item_note(source_app: str, date: str, superseded: bool, revived: bool) -> str:
    parts = [f"来自应用「{source_app}」" if source_app else "", date]
    if superseded:
        parts.append(SUPERSEDED_NOTE)
    if revived:
        parts.append(REVIVED_NOTE)
    parts = [part for part in parts if part]
    return f"（{'·'.join(parts)}）" if parts else ""


def extract_memories(payload: dict) -> list[dict]:
    memories = payload.get("memories")
    if not isinstance(memories, list):
        return []
    return [memory for memory in memories if isinstance(memory, dict)]


def is_guest(user_id: str) -> bool:
    return str(user_id or "").strip().lower() in GUEST_IDS


__all__ = [
    "FOOTER",
    "GUEST_IDS",
    "RECALL_HEADER",
    "SNAPSHOT_HEADER",
    "as_date",
    "as_str",
    "extract_memories",
    "is_guest",
    "item_note",
]
