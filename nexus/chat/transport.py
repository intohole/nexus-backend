"""聊天传输抽象 — 统一 SSE 流式与 JSON 非流式响应格式。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from fastapi import Request

from nexus.streaming import sse_chat_stream_v2


class ChatTransport(Protocol):
    async def stream(
        self, events: AsyncIterator[str | dict], request: Request | None = None
    ) -> Any: ...
    async def respond(self, result: dict[str, Any]) -> Any: ...


class SSETransport:
    async def stream(
        self, events: AsyncIterator[str | dict], request: Request | None = None
    ) -> Any:
        return sse_chat_stream_v2(events, request=request)

    async def respond(self, result: dict[str, Any]) -> Any:
        from nexus.response import success_response

        return success_response(result)


class JSONTransport:
    async def stream(
        self, events: AsyncIterator[str | dict], request: Request | None = None
    ) -> Any:
        collected: list[str] = []
        async for event in events:
            if isinstance(event, dict):
                if event.get("type") == "delta":
                    collected.append(str(event.get("content", "")))
                elif event.get("type") == "error":
                    from nexus.response import error_response

                    return error_response(str(event.get("message", "error")), code=500)
            else:
                collected.append(str(event))
        from nexus.response import success_response

        return success_response({"content": "".join(collected)})

    async def respond(self, result: dict[str, Any]) -> Any:
        from nexus.response import success_response

        return success_response(result)
