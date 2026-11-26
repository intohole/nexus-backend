from __future__ import annotations

import logging
from dataclasses import dataclass

from ironman.memory.layers import LayeredMemory
from ironman.types import Message, Role

from nexus.bee_memory import BeeMemoryBackend
from nexus.memory_common import (
    FOOTER,
    SNAPSHOT_HEADER,
    as_date,
    as_str,
    extract_memories,
    is_guest,
    item_note,
)
from nexus.memory_contract import MemoryBudget

logger = logging.getLogger("nexus.harness_working")

HISTORY_HEADER = "以下是用户近期对话的记录（{count} 条），延续语境用："
WORKING_TRUNCATION_NOTE = "（另有 {dropped} 条上下文因预算截断，未注入）"


@dataclass(frozen=True)
class WorkingContext:
    history: tuple[Message, ...] = ()
    text: str = ""
    history_count: int = 0
    truncated: bool = False


def history_messages(raw: object) -> list[Message]:
    if not isinstance(raw, list):
        return []
    messages: list[Message] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if not content or role not in {Role.USER.value, Role.ASSISTANT.value, Role.SYSTEM.value}:
            continue
        messages.append(Message(role=Role(role), content=content))
    return messages


def render_working_context_bounded(
    payload: dict, *, self_app: str = "", budget_chars: int | None = None
) -> tuple[str, dict[str, object]]:
    readout: dict[str, object] = {
        "history_items": 0,
        "memory_items": 0,
        "n_chars": 0,
        "truncated": False,
    }
    if payload.get("unavailable") or payload.get("success") is False or payload.get("error"):
        return "", readout
    history_lines: list[str] = []
    history = payload.get("history_window")
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            content = as_str(item.get("content"))
            if content:
                history_lines.append(f"- {item.get('role') or 'user'}: {content}")
    memory_lines: list[str] = []
    memories = extract_memories(payload)
    if memories:
        self_app = self_app or as_str(payload.get("app_name"))
        for memory in memories:
            content = as_str(memory.get("content"))
            if not content:
                continue
            source_app = as_str(memory.get("source_app"))
            if source_app == self_app:
                source_app = ""
            note = item_note(source_app, as_date(memory.get("created_at")), False, False)
            memory_lines.append(f"- {content}{note}")
    readout["history_items"] = len(history_lines)
    readout["memory_items"] = len(memory_lines)
    if not history_lines and not memory_lines:
        return "", readout
    kept_history = history_lines
    kept_memory = memory_lines
    dropped = 0
    if budget_chars is not None and budget_chars > 0:
        room = budget_chars - len(FOOTER) - 2
        if history_lines:
            room -= len(HISTORY_HEADER.format(count=len(history_lines))) + 1
        if memory_lines:
            room -= len(SNAPSHOT_HEADER.format(count=len(memory_lines))) + 1
        kept_history = []
        for line in reversed(history_lines):
            cost = len(line) + 1
            if cost > room:
                break
            kept_history.append(line)
            room -= cost
        kept_history.reverse()
        kept_memory = []
        for line in memory_lines:
            cost = len(line) + 1
            if cost > room:
                break
            kept_memory.append(line)
            room -= cost
        dropped = (len(history_lines) - len(kept_history)) + (len(memory_lines) - len(kept_memory))
    sections: list[str] = []
    if kept_history:
        sections.append(HISTORY_HEADER.format(count=len(kept_history)) + "\n" + "\n".join(kept_history))
    if kept_memory:
        sections.append(SNAPSHOT_HEADER.format(count=len(kept_memory)) + "\n" + "\n".join(kept_memory))
    if not sections:
        readout["truncated"] = dropped > 0
        return "", readout
    text = "\n\n".join(sections) + "\n"
    if dropped > 0:
        text += WORKING_TRUNCATION_NOTE.format(dropped=dropped) + "\n"
    text += FOOTER
    readout["n_chars"] = len(text)
    readout["truncated"] = dropped > 0
    return text, readout


def render_working_context(payload: dict, *, self_app: str = "") -> str:
    text, _readout = render_working_context_bounded(payload, self_app=self_app)
    return text


async def working_context(
    user_id: str,
    *,
    sdk: object,
    app_name: str = "",
    session_id: str = "",
    history_turns: int = 12,
    self_app: str = "",
) -> str:
    """工作上下文一行式（r27）：get_context + render_working_context，任何失败返回空串。

    业务方一次调用拿到「近窗对话+长期画像」注入块，替代自行拼装
    recall_context + 历史拉取的双轨样板。
    """
    uid = str(user_id or "").strip()
    if is_guest(uid):
        return ""
    get_ctx = getattr(sdk, "get_context", None)
    if get_ctx is None:
        return ""
    try:
        payload = await get_ctx(
            user_id=uid,
            app_name=app_name or None,
            session_id=session_id or None,
            history_turns=history_turns,
        )
    except Exception as exc:
        logger.warning("memory working_context failed, skip injection: %s", exc)
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("unavailable") or payload.get("success") is False:
        logger.warning("beeMemory unavailable: %s", payload.get("message", ""))
        return ""
    return render_working_context(payload, self_app=self_app)


async def read_working_context(
    backend: BeeMemoryBackend | None,
    app_name: str,
    history_turns: int = 12,
    top_k: int = 10,
    max_chars: int = 1200,
    history_max_chars: int | None = None,
    budget: MemoryBudget | None = None,
) -> WorkingContext:
    if backend is None:
        return WorkingContext()
    contract = budget or backend.budget
    payload = await backend.context(
        history_turns=history_turns,
        top_k=top_k,
        max_chars=max_chars,
        history_max_chars=history_max_chars,
        budget=contract,
    )
    if not payload:
        return WorkingContext()
    history = history_messages(payload.get("history_window"))
    text, readout = render_working_context_bounded(
        payload,
        self_app=app_name,
        budget_chars=contract.max_chars or max_chars,
    )
    contract.ensure(readout)
    return WorkingContext(
        history=tuple(history),
        text=text,
        history_count=len(history),
        truncated=bool(readout.get("truncated")),
    )


async def restore_working_memory(
    memory: LayeredMemory,
    backend: BeeMemoryBackend | None,
    app_name: str,
    history_turns: int = 12,
    top_k: int = 10,
    max_chars: int = 1200,
    history_max_chars: int | None = None,
    budget: MemoryBudget | None = None,
) -> int:
    if await memory.size() > 0:
        return 0
    context = await read_working_context(
        backend,
        app_name,
        history_turns=history_turns,
        top_k=top_k,
        max_chars=max_chars,
        history_max_chars=history_max_chars,
        budget=budget,
    )
    for message in context.history:
        await memory.add(message)
    return len(context.history)


__all__ = [
    "WorkingContext",
    "history_messages",
    "read_working_context",
    "render_working_context",
    "render_working_context_bounded",
    "restore_working_memory",
    "working_context",
]
