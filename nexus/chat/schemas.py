from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    status: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    status: str
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    meta: dict[str, Any]
    created_at: datetime
