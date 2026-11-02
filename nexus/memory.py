"""记忆上下文原语 — beeMemory 召回信封到 LLM 注入块的单源渲染与 fail-open 辅助。

注入块逐字节稳定纪律（对齐 S82 前缀纪律）：块内只含内容、来源应用、日期（YYYY-MM-DD）
与状态标记，不含分数/相对时间等请求级或时变值；空结果恒返回空串由调用方决定静默。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.memory")

_GUEST_IDS = ("", "guest", "anonymous")

_RECALL_HEADER = "以下是用户的历史记忆（命中 {count} 条），帮助更贴合地回答："
_SNAPSHOT_HEADER = "以下是用户的长期记忆画像（最相关 {count} 条）："
_FOOTER = "使用指引：以上记忆仅供理解用户背景，不要逐条复述；与用户本轮表述冲突时，以本轮表述为准。"
_SUPERSEDED_NOTE = "已被用户后来的表述更新"
_REVIVED_NOTE = "重新启用的较早记忆"


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_date(value: object) -> str:
    text = _as_str(value)
    return text[:10] if len(text) >= 10 else ""


def _item_note(source_app: str, date: str, superseded: bool, revived: bool) -> str:
    parts = [f"来自应用「{source_app}」" if source_app else "", date]
    if superseded:
        parts.append(_SUPERSEDED_NOTE)
    if revived:
        parts.append(_REVIVED_NOTE)
    parts = [p for p in parts if p]
    return f"（{'·'.join(parts)}）" if parts else ""


def _extract_memories(payload: dict) -> list[dict]:
    memories = payload.get("memories")
    if not isinstance(memories, list):
        return []
    return [m for m in memories if isinstance(m, dict)]


def render_memory_context(payload: dict, *, self_app: str = "", kind: str = "") -> str:
    """beeMemory v1/recall 或 v1/snapshot 信封 → 注入文本块；空/错误信封返回空串。

    kind 缺省自动识别：信封带 empty_reason/analysis 视为 recall，否则 snapshot。
    self_app 传入调用方自身 app 名时，同源记忆不标注来源（跨应用来源才标注）。
    """
    if payload.get("unavailable") or payload.get("success") is False or payload.get("error"):
        return ""
    memories = _extract_memories(payload)
    if not memories:
        return ""
    if not kind:
        kind = "recall" if ("empty_reason" in payload or "analysis" in payload) else "snapshot"
    lines: list[str] = []
    for memory in memories:
        content = _as_str(memory.get("content") or memory.get("memory_content"))
        if not content:
            continue
        source_app = _as_str(memory.get("source_app"))
        if source_app == self_app:
            source_app = ""
        note = _item_note(
            source_app,
            _as_date(memory.get("created_at")),
            bool(memory.get("superseded")),
            bool(memory.get("revived")),
        )
        lines.append(f"- {content}{note}")
    if not lines:
        return ""
    count = len(lines)
    header = _SNAPSHOT_HEADER if kind == "snapshot" else _RECALL_HEADER
    return "\n".join([header.format(count=count)] + lines + [_FOOTER])


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
) -> str:
    """召回并渲染记忆块的一行式辅助：任何失败（不可用/异常/空结果）返回空串。

    空结果分型记入日志（no_scope=用户无记忆无需重试 / no_match=有记忆不匹配），
    供上游按 S47 决策行口径补业务上下文。
    """
    uid = str(user_id or "").strip()
    if uid.lower() in _GUEST_IDS or not str(content or "").strip():
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
    empty_reason = _as_str(result.get("empty_reason"))
    if empty_reason:
        logger.info("memory recall empty [user=%s] reason=%s hint=%s", uid, empty_reason, _as_str(result.get("hint")))
        return ""
    return render_memory_context(result, self_app=self_app, kind="recall")


def empty_reason_of(payload: dict) -> str:
    """读取召回信封的空结果分型（非空信封返回空串）。"""
    if _extract_memories(payload):
        return ""
    return _as_str(payload.get("empty_reason"))


__all__ = ["render_memory_context", "recall_context", "empty_reason_of"]
