from __future__ import annotations

import logging
from datetime import datetime

from ironman.memory.layers import MemoryItem, MemoryScope
from nexus.bm_sdk import BeeMemorySDK, get_bm_sdk
from nexus.memory import render_memory_context_bounded

logger = logging.getLogger(__name__)

_PRIORITY_IMPORTANCE = {"high": 0.8, "medium": 0.5, "low": 0.2}


class BeeMemoryBackend:
    def __init__(
        self,
        user_id: str,
        app_name: str,
        sdk: BeeMemorySDK | None = None,
        session_id: str = "",
        tenant_id: str = "",
        memory_scope: str = "",
    ) -> None:
        self._user_id = user_id
        self._app_name = app_name
        self._sdk = sdk or get_bm_sdk(app_name=app_name)
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._memory_scope = memory_scope

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def app_name(self) -> str:
        return self._app_name

    async def recall(self, query: str, scope: MemoryScope | None = None, limit: int = 5) -> list[MemoryItem]:
        if not self._user_id or not query.strip() or limit <= 0:
            return []
        try:
            payload = await self._sdk.recall_memories(
                content=query,
                user_id=self._user_id,
                app_name=self._app_name,
                top_k=limit,
                smart=True,
            )
        except Exception as exc:
            logger.warning("beeMemory recall 失败，降级为空: %s", exc)
            return []
        return self._items(payload if isinstance(payload, dict) else {}, scope or MemoryScope.USER)

    async def snapshot(self, top_k: int = 10, max_chars: int = 1200) -> dict[str, object]:
        try:
            payload = await self._sdk.snapshot_memories(
                user_id=self._user_id,
                app_name=self._app_name,
                top_k=top_k,
                max_chars=max_chars,
            )
        except Exception as exc:
            logger.warning("beeMemory snapshot 失败，降级为空: %s", exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def render_snapshot(self, top_k: int = 10, max_chars: int = 1200) -> str:
        payload = await self.snapshot(top_k=top_k, max_chars=max_chars)
        text, _readout = render_memory_context_bounded(
            payload,
            self_app=self._app_name,
            kind="snapshot",
            budget_chars=max_chars,
        )
        return text

    async def context(
        self,
        history_turns: int = 12,
        top_k: int = 10,
        max_chars: int = 1200,
        history_max_chars: int | None = None,
    ) -> dict[str, object]:
        try:
            payload = await self._sdk.get_context(
                user_id=self._user_id,
                app_name=self._app_name,
                session_id=self._session_id,
                history_turns=history_turns,
                top_k=top_k,
                max_chars=max_chars,
                history_max_chars=history_max_chars,
            )
        except Exception as exc:
            logger.warning("beeMemory context 失败，降级为空: %s", exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def add(
        self,
        content: str,
        scope: MemoryScope | None = None,
        session_id: str = "",
        channel: str = "",
    ) -> dict[str, object]:
        if not self._user_id or not content.strip():
            return {"success": False, "message": "missing user_id or content"}
        try:
            payload = await self._sdk.add_memory(
                content=content,
                user_id=self._user_id,
                app_name=self._app_name,
                session_id=session_id or self._session_id,
                channel=channel,
            )
        except Exception as exc:
            logger.warning("beeMemory add 失败，降级跳过: %s", exc)
            return {"success": False, "message": str(exc)}
        return payload if isinstance(payload, dict) else {}

    async def save_history(self, messages: list[dict[str, str]], channel: str = "") -> dict[str, object]:
        try:
            payload = await self._sdk.save_history(
                user_id=self._user_id,
                messages=messages,
                app_name=self._app_name,
                session_id=self._session_id,
                channel=channel,
            )
        except Exception as exc:
            logger.warning("beeMemory save_history 失败: %s", exc)
            return {"success": False, "message": str(exc)}
        return payload if isinstance(payload, dict) else {}

    async def health(self) -> bool:
        try:
            return await self._sdk.health_check()
        except Exception:
            return False

    def _items(self, payload: dict[str, object], scope: MemoryScope) -> list[MemoryItem]:
        if payload.get("unavailable") or payload.get("success") is False or payload.get("error"):
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
            importance = self._importance(entry)
            items.append(
                MemoryItem(
                    content=content,
                    scope=scope,
                    key=str(entry.get("id") or ""),
                    importance=importance,
                    created_at=self._timestamp(entry.get("created_at")),
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

    def _importance(self, entry: dict[str, object]) -> float:
        score = entry.get("score")
        if isinstance(score, (int, float)):
            return max(0.0, min(1.0, float(score)))
        priority = str(entry.get("priority") or "medium").lower()
        return _PRIORITY_IMPORTANCE.get(priority, 0.5)

    def _timestamp(self, value: object) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        return 0.0
