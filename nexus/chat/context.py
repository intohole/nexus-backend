from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus.chat.models import ChatConversation


@dataclass
class ChatContext:
    conversation: ChatConversation
    user_message: str
    context_parts: list[str] = field(default_factory=list)
    history_messages: list[dict[str, str]] = field(default_factory=list)
    default_title: str = "新对话"

    @property
    def meta(self) -> dict[str, Any]:
        return self.conversation.meta or {}
