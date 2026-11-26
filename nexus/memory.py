"""记忆上下文原语 — beeMemory 召回信封到 LLM 注入块的单源渲染与 fail-open 辅助。

注入块逐字节稳定纪律（对齐 S82 前缀纪律）：块内只含内容、来源应用、日期（YYYY-MM-DD）
与状态标记，不含分数/相对时间等请求级或时变值；空结果恒返回空串由调用方决定静默。
"""
from __future__ import annotations

import logging

from nexus.memory_common import (
    FOOTER,
    MEMORY_FENCE_CLOSE,
    MEMORY_FENCE_OPEN,
    RECALL_HEADER,
    SNAPSHOT_HEADER,
    as_date,
    as_str,
    extract_memories,
    fence_memory_block,
    is_guest,
    item_note,
)

logger = logging.getLogger("nexus.memory")

_TRUNCATION_NOTE = "（另有 {dropped} 条记忆因预算截断，未注入）"


def render_memory_context(payload: dict, *, self_app: str = "", kind: str = "") -> str:
    """beeMemory v1/recall 或 v1/snapshot 信封 → 注入文本块；空/错误信封返回空串。

    kind 缺省自动识别：信封带 empty_reason/analysis 视为 recall，否则 snapshot。
    self_app 传入调用方自身 app 名时，同源记忆不标注来源（跨应用来源才标注）。
    """
    text, _readout = render_memory_context_bounded(payload, self_app=self_app, kind=kind)
    return text


def render_memory_context_bounded(
    payload: dict, *, self_app: str = "", kind: str = "", budget_chars: int | None = None
) -> tuple[str, dict]:
    """带注入预算的渲染（S176 AM4）：超限分层截断（尾部条目先截）+注入量读数。

    返回 (注入文本, 读数{n_items, n_chars, truncated})；budget_chars 为 None 时
    全量渲染（等价老 render_memory_context 行为）。截断时保留头尾框架与截断尾标。
    """
    readout = {"n_items": 0, "n_chars": 0, "truncated": False}
    if payload.get("unavailable") or payload.get("success") is False or payload.get("error"):
        return "", readout
    memories = extract_memories(payload)
    if not memories:
        return "", readout
    if not kind:
        kind = "recall" if ("empty_reason" in payload or "analysis" in payload) else "snapshot"
    lines: list[str] = []
    for memory in memories:
        content = as_str(memory.get("content") or memory.get("memory_content"))
        if not content:
            continue
        source_app = as_str(memory.get("source_app"))
        if source_app == self_app:
            source_app = ""
        note = item_note(
            source_app,
            as_date(memory.get("created_at")),
            bool(memory.get("superseded")),
            bool(memory.get("revived")),
        )
        lines.append(f"- {content}{note}")
    if not lines:
        return "", readout
    header = SNAPSHOT_HEADER if kind == "snapshot" else RECALL_HEADER
    footer_len = len(FOOTER)
    fence_cost = len(MEMORY_FENCE_OPEN) + len(MEMORY_FENCE_CLOSE) + 2
    kept = lines
    dropped = 0
    if budget_chars is not None and budget_chars > 0:
        overhead = len(header.format(count=len(lines))) + footer_len + fence_cost + 8
        room = budget_chars - overhead
        kept = []
        used = 0
        for line in lines:
            cost = len(line) + 1
            if used + cost > room:
                break
            kept.append(line)
            used += cost
        dropped = len(lines) - len(kept)
        if dropped > 0 and kept:
            room -= len(_TRUNCATION_NOTE.format(dropped=dropped)) + 1
            while kept and used > room:
                used -= len(kept[-1]) + 1
                kept.pop()
                dropped += 1
    parts = [header.format(count=len(kept))] + kept
    if dropped > 0:
        parts.append(_TRUNCATION_NOTE.format(dropped=dropped))
    parts.append(FOOTER)
    text = fence_memory_block("\n".join(parts))
    readout = {
        "n_items": len(kept),
        "n_chars": len(text),
        "truncated": dropped > 0,
    }
    return text, readout


async def recall_context(
    user_id: str,
    content: str,
    *,
    sdk: object,
    app_name: str = "",
    top_k: int = 4,
    max_chars: int = 2000,
    self_app: str = "",
    smart: bool = True,
    budget_chars: int | None = None,
) -> str:
    """召回并渲染记忆块的一行式辅助：任何失败（不可用/异常/空结果）返回空串。

    空结果分型记入日志（no_scope=用户无记忆无需重试 / no_match=有记忆不匹配），
    供上游按 S47 决策行口径补业务上下文。
    """
    uid = str(user_id or "").strip()
    if is_guest(uid) or not str(content or "").strip():
        return ""
    recall = getattr(sdk, "recall_memories", None)
    if recall is None:
        return ""
    try:
        result = await recall(
            content=content,
            user_id=uid,
            app_name=app_name or None,
            top_k=top_k,
            max_chars=max_chars,
            smart=smart,
        )
    except Exception as exc:
        logger.warning("memory recall failed, skip injection: %s", exc)
        return ""
    if not isinstance(result, dict):
        return ""
    if result.get("unavailable") or result.get("success") is False:
        logger.warning("beeMemory unavailable: %s", result.get("message", ""))
        return ""
    empty_reason = as_str(result.get("empty_reason"))
    if empty_reason:
        logger.info("memory recall empty [user=%s] reason=%s hint=%s", uid, empty_reason, as_str(result.get("hint")))
        return ""
    text, readout = render_memory_context_bounded(
        result, self_app=self_app, kind="recall", budget_chars=budget_chars)
    if readout["n_items"]:
        logger.info(
            "memory injection readout [user=%s app=%s] items=%s chars=%s truncated=%s budget=%s",
            uid, app_name or "-", readout["n_items"], readout["n_chars"],
            readout["truncated"], budget_chars,
        )
    return text


def empty_reason_of(payload: dict) -> str:
    """读取召回信封的空结果分型（非空信封返回空串）。"""
    if extract_memories(payload):
        return ""
    return as_str(payload.get("empty_reason"))


__all__ = [
    "empty_reason_of",
    "recall_context",
    "render_memory_context",
    "render_memory_context_bounded",
]
