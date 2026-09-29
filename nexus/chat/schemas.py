"""聊天 API 契约 — 统一会话与消息的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    meta: dict[str, Any] | None = None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)
