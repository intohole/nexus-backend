"""聊天存储抽象 — 定义 ChatStore 契约与 LocalChatStore 本地实现。

Phase 1 各项目注入 LocalChatStore 使用本地表; Phase 4 切换 CentralChatStore
实现跨应用会话连续, 业务代码无需改动。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nexus.chat.models import ChatBase, ChatConversation, ChatMessage, _utcnow


class ChatStore(Protocol):
    async def create_conversation(self, conversation: ChatConversation) -> ChatConversation: ...
    async def get_conversation(self, conversation_id: str) -> ChatConversation | None: ...
    async def list_conversations(
        self, user_id: str, app_name: str, status: str | None, page: int, page_size: int
    ) -> dict[str, Any]: ...
    async def update_conversation(
        self, conversation_id: str, **fields: Any
    ) -> ChatConversation | None: ...
    async def delete_conversation(self, conversation_id: str) -> bool: ...
    async def search_conversations(
        self, user_id: str, app_name: str, query: str, limit: int
    ) -> list[ChatConversation]: ...
    async def add_message(
        self, conversation_id: str, role: str, content: str, meta: dict[str, Any] | None = None
    ) -> ChatMessage: ...
    async def list_messages(
        self, conversation_id: str, page: int, page_size: int
    ) -> dict[str, Any]: ...
    async def recent_messages(self, conversation_id: str, limit: int) -> list[ChatMessage]: ...
    async def count_messages(self, conversation_id: str) -> int: ...


class LocalChatStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._initialized: bool = False

    async def _ensure_tables(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with self._engine.begin() as conn:
                await conn.run_sync(ChatBase.metadata.create_all)
            self._initialized = True

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[AsyncSession, None]:
        await self._ensure_tables()
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_conversation(self, conversation: ChatConversation) -> ChatConversation:
        async with self._session() as session:
            session.add(conversation)
            await session.flush()
            return conversation

    async def get_conversation(self, conversation_id: str) -> ChatConversation | None:
        async with self._session() as session:
            return await session.get(ChatConversation, conversation_id)

    async def list_conversations(
        self, user_id: str, app_name: str, status: str | None, page: int, page_size: int
    ) -> dict[str, Any]:
        async with self._session() as session:
            query = select(ChatConversation).where(
                ChatConversation.user_id == user_id,
                ChatConversation.app_name == app_name,
            )
            if status:
                query = query.where(ChatConversation.status == status)
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            result = await session.execute(
                query.order_by(ChatConversation.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {"items": list(result.scalars().all()), "total": int(total or 0)}

    async def update_conversation(
        self, conversation_id: str, **fields: Any
    ) -> ChatConversation | None:
        async with self._session() as session:
            conversation = await session.get(ChatConversation, conversation_id)
            if conversation is None:
                return None
            for key, value in fields.items():
                if hasattr(conversation, key):
                    setattr(conversation, key, value)
            await session.flush()
            return conversation

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with self._session() as session:
            conversation = await session.get(ChatConversation, conversation_id)
            if conversation is None:
                return False
            await session.execute(
                ChatMessage.__table__.delete().where(ChatMessage.conversation_id == conversation_id)
            )
            await session.delete(conversation)
            return True

    async def search_conversations(
        self, user_id: str, app_name: str, query: str, limit: int
    ) -> list[ChatConversation]:
        async with self._session() as session:
            pattern = f"%{query}%"
            result = await session.execute(
                select(ChatConversation)
                .where(
                    ChatConversation.user_id == user_id,
                    ChatConversation.app_name == app_name,
                    or_(
                        ChatConversation.title.like(pattern),
                        ChatConversation.id.like(pattern),
                    ),
                )
                .order_by(ChatConversation.updated_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def add_message(
        self, conversation_id: str, role: str, content: str, meta: dict[str, Any] | None = None
    ) -> ChatMessage:
        async with self._session() as session:
            message = ChatMessage(
                conversation_id=conversation_id,
                role=role,
                content=content,
                meta=meta or {},
            )
            session.add(message)
            await session.flush()
            conversation = await session.get(ChatConversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = _utcnow()
            return message

    async def list_messages(
        self, conversation_id: str, page: int, page_size: int
    ) -> dict[str, Any]:
        async with self._session() as session:
            query = select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            result = await session.execute(
                query.order_by(ChatMessage.created_at.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {"items": list(result.scalars().all()), "total": int(total or 0)}

    async def recent_messages(self, conversation_id: str, limit: int) -> list[ChatMessage]:
        async with self._session() as session:
            result = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(limit)
            )
            messages = list(result.scalars().all())
            messages.reverse()
            return messages

    async def count_messages(self, conversation_id: str) -> int:
        async with self._session() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
            )
            return int(total or 0)
