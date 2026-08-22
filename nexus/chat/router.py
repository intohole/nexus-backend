from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from nexus.auth import get_current_user_id_required
from nexus.chat.engine import ChatEngine
from nexus.chat.schemas import ConversationCreate, ConversationUpdate, MessageCreate
from nexus.response import paginate_response, success_response


def _conv_out(conversation: Any) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "status": conversation.status,
        "meta": conversation.meta or {},
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
    }


def _msg_out(message: Any) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "meta": message.meta or {},
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def chat_router(engine: ChatEngine, app_name: str) -> APIRouter:
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.post("/conversations")
    async def create_conversation(
        payload: ConversationCreate,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        conversation = await engine.create_conversation(user_id, app_name, payload.title)
        return success_response(_conv_out(conversation))

    @router.get("/conversations")
    async def list_conversations(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        status: str | None = None,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        result = await engine.list_conversations(user_id, app_name, status, page, page_size)
        items = [_conv_out(c) for c in result["items"]]
        return paginate_response(items, result["total"], page, page_size)

    @router.get("/conversations/search")
    async def search_conversations(
        q: str,
        limit: int = Query(20, ge=1, le=100),
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        items = await engine.search_conversations(user_id, app_name, q, limit)
        return success_response([_conv_out(c) for c in items])

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        conversation = await engine.get_conversation(user_id, conversation_id)
        return success_response(_conv_out(conversation))

    @router.patch("/conversations/{conversation_id}")
    async def update_conversation(
        conversation_id: str,
        payload: ConversationUpdate,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        fields = payload.model_dump(exclude_none=True)
        conversation = await engine.update_conversation(user_id, conversation_id, **fields)
        return success_response(_conv_out(conversation))

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        await engine.delete_conversation(user_id, conversation_id)
        return success_response({"deleted": True})

    @router.post("/conversations/{conversation_id}/messages")
    async def send_message(
        conversation_id: str,
        payload: MessageCreate,
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        result = await engine.send_message(user_id, app_name, conversation_id, payload.content)
        return success_response(result)

    @router.post("/conversations/{conversation_id}/messages/stream")
    async def stream_message(
        conversation_id: str,
        payload: MessageCreate,
        request: Request,
        user_id: str = Depends(get_current_user_id_required),
    ) -> Any:
        events = engine.stream_message(user_id, app_name, conversation_id, payload.content)
        return await engine.transport.stream(events, request=request)

    @router.get("/conversations/{conversation_id}/messages")
    async def list_messages(
        conversation_id: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        result = await engine.list_messages(user_id, conversation_id, page, page_size)
        items = [_msg_out(m) for m in result["items"]]
        return paginate_response(items, result["total"], page, page_size)

    return router
