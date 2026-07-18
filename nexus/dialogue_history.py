"""对话历史管理 — 三种策略 + 可插拔存储后端。

参考 2026 上下文工程最佳实践：
- 全量保留（≤10 轮）：短会话直接保留所有 turns
- 滑动窗口（>10 轮）：保留最近 N 轮，丢弃更早的
- 摘要压缩（>30 轮）：调 LLM 总结前 N 轮为 system message

避免过度设计：仅在多轮对话场景使用；单轮 Q&A 无需引入。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.dialogue_history")


class HistoryStrategy(str, Enum):
    """历史保留策略。"""

    FULL = "full"              # 全量保留（≤ max_turns 时默认）
    SLIDING_WINDOW = "sliding"  # 滑动窗口（超过 max_turns 触发）
    SUMMARIZE = "summarize"     # 摘要压缩（超过 summarize_threshold 触发）


class HistoryStore(ABC):
    """对话历史存储后端抽象。"""

    @abstractmethod
    async def load(self, session_id: str) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        ...


class InMemoryStore(HistoryStore):
    """进程内 dict 存储，重启即丢。仅适用于短会话或测试。"""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._data.get(session_id, []))

    async def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._data[session_id] = list(messages)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)


class SQLiteStore(HistoryStore):
    """SQLite 持久化存储，aiosqlite 异步驱动。

    表结构：sessions(session_id TEXT PK, messages TEXT, updated_at REAL)
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        import aiosqlite

        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_id TEXT PRIMARY KEY,
                    messages TEXT NOT NULL DEFAULT '[]',
                    summary TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def load(self, session_id: str) -> list[dict[str, Any]]:
        import aiosqlite

        await self._ensure_schema()
        async with self._lock, aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT messages FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return []
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Corrupted history for session %s, resetting", session_id)
                return []

    async def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        import aiosqlite

        await self._ensure_schema()
        async with self._lock, aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO conversation_sessions (session_id, messages, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages = excluded.messages,
                    updated_at = excluded.updated_at
                """,
                (session_id, json.dumps(messages, ensure_ascii=False), time.time()),
            )
            await db.commit()

    async def delete(self, session_id: str) -> None:
        import aiosqlite

        await self._ensure_schema()
        async with self._lock, aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            )
            await db.commit()


class ConversationHistory:
    """单会话对话历史管理器。

    使用方式：
        history = ConversationHistory(session_id="user_123", store=SQLiteStore(...))
        await history.add(user_msg="你好", assistant_msg="你好，有什么可以帮您？")
        messages = await history.get_messages()  # OpenAI messages 格式
    """

    def __init__(
        self,
        session_id: str,
        store: Optional[HistoryStore] = None,
        max_turns: int = 10,
        summarize_threshold: int = 30,
        summarize_fn: Optional[Any] = None,
    ) -> None:
        self.session_id = session_id
        self._store = store or InMemoryStore()
        self.max_turns = max_turns
        self.summarize_threshold = summarize_threshold
        self._summarize_fn = summarize_fn  # async Callable[list[dict]] -> str
        self._cached: Optional[list[dict[str, Any]]] = None
        self._summary: Optional[str] = None

    async def add(
        self,
        user_msg: str,
        assistant_msg: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        messages = await self._load()
        messages.append({"role": "user", "content": user_msg, "metadata": metadata or {}})
        messages.append({"role": "assistant", "content": assistant_msg})
        messages = await self._apply_strategy(messages)
        await self._store.save(self.session_id, messages)
        self._cached = messages

    async def get_messages(self) -> list[dict[str, str]]:
        """返回 OpenAI messages 格式（仅 role + content）。"""
        messages = await self._load()
        result: list[dict[str, str]] = []
        if self._summary:
            result.append({"role": "system", "content": self._summary})
        for msg in messages:
            result.append({"role": msg["role"], "content": msg["content"]})
        return result

    async def get_summary(self) -> Optional[str]:
        return self._summary

    async def clear(self) -> None:
        await self._store.delete(self.session_id)
        self._cached = None
        self._summary = None

    async def _load(self) -> list[dict[str, Any]]:
        if self._cached is not None:
            return list(self._cached)
        self._cached = await self._store.load(self.session_id)
        return list(self._cached)

    async def _apply_strategy(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """根据消息数量自动选择策略。"""
        total_turns = len(messages) // 2
        if total_turns <= self.max_turns:
            return messages  # FULL

        if total_turns > self.summarize_threshold and self._summarize_fn:
            return await self._summarize_old(messages)

        # SLIDING_WINDOW: 保留最近 max_turns 轮
        keep_count = self.max_turns * 2
        return messages[-keep_count:]

    async def _summarize_old(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """调 LLM 总结前 N 轮，保留最近 max_turns 轮。"""
        keep_count = self.max_turns * 2
        to_summarize = messages[:-keep_count]
        to_keep = messages[-keep_count:]

        try:
            summary_chunk = await self._summarize_fn(to_summarize)
            existing = self._summary or ""
            self._summary = (existing + "\n" + summary_chunk).strip() if existing else summary_chunk
            logger.info(
                "Summarized %d messages for session %s (summary len=%d)",
                len(to_summarize), self.session_id, len(self._summary or ""),
            )
        except Exception as exc:
            logger.warning("Summarization failed, falling back to sliding window: %s", exc)
            return messages[-keep_count:]

        return to_keep


_history_registry: dict[str, ConversationHistory] = {}
_registry_lock = asyncio.Lock()


async def get_history(
    session_id: str,
    store: Optional[HistoryStore] = None,
    max_turns: int = 10,
    summarize_threshold: int = 30,
    summarize_fn: Optional[Any] = None,
) -> ConversationHistory:
    """获取或创建会话历史单例。

    相同 session_id 多次调用返回同一实例，store/max_turns 等参数仅在首次创建时生效。
    """
    async with _registry_lock:
        if session_id in _history_registry:
            return _history_registry[session_id]
        history = ConversationHistory(
            session_id=session_id,
            store=store,
            max_turns=max_turns,
            summarize_threshold=summarize_threshold,
            summarize_fn=summarize_fn,
        )
        _history_registry[session_id] = history
        return history


def clear_history_cache(session_id: Optional[str] = None) -> None:
    """清理历史缓存（主要用于测试或会话结束）。"""
    if session_id:
        _history_registry.pop(session_id, None)
    else:
        _history_registry.clear()


__all__ = [
    "HistoryStrategy",
    "HistoryStore",
    "InMemoryStore",
    "SQLiteStore",
    "ConversationHistory",
    "get_history",
    "clear_history_cache",
]