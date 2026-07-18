"""SSE 流式响应助手 — 统一对话流式输出协议。

所有 app 的 chat_stream 端点应使用 `sse_chat_stream` 包装异步生成器，
避免每个 app 各写一套 SSE 协议。

事件格式（遵循 SSE 规范）：
    data: {"event": "delta", "content": "..."}\n\n
    data: {"event": "done", "content": "完整文本"}\n\n
    data: {"event": "error", "error": "..."}\n\n

若 ironman 暂不支持原生 streaming，调用方可先用 `chunked_text_stream`
将完整文本切块后 yield，模拟流式 UX。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.streaming")


def sse_event(event: str, data: Optional[dict[str, Any]] = None) -> str:
    """格式化单个 SSE 事件。"""
    payload = {"event": event}
    if data:
        payload.update(data)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def chunked_text_stream(
    text: str,
    chunk_size: int = 8,
    delay: float = 0.03,
) -> AsyncIterator[str]:
    """将完整文本切为小块 yield，模拟流式输出。

    用于 ironman 无原生 streaming 时的降级方案。
    chunk_size 默认 8 个字符（中文按字数感知更自然），delay 30ms 接近真实打字机节奏。
    """
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]
        if delay > 0:
            await asyncio.sleep(delay)


async def _sse_generator(
    chat_fn: AsyncIterator[str],
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
) -> AsyncIterator[str]:
    """将文本 chunk 流包装为 SSE 事件流。"""
    accumulated: list[str] = []
    try:
        async for chunk in chat_fn:
            if not chunk:
                continue
            accumulated.append(chunk)
            yield sse_event("delta", {"content": chunk})
        full_content = "".join(accumulated)
        if on_complete:
            try:
                await on_complete(full_content)
            except Exception as exc:
                logger.warning("on_complete callback failed: %s", exc)
        yield sse_event("done", {"content": full_content})
    except Exception as exc:
        logger.error("SSE stream error: %s", exc)
        yield sse_event("error", {"error": str(exc)})


def sse_chat_stream(
    chat_fn: AsyncIterator[str],
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
):
    """将异步生成器包装为 FastAPI StreamingResponse。

    用法：
        async def my_chat_stream(msg: str) -> AsyncIterator[str]:
            # 调用 LLM，yield 每个 chunk
            ...
        return sse_chat_stream(my_chat_stream(user_msg))

    返回的 StreamingResponse media_type 为 text/event-stream。
    """
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        _sse_generator(chat_fn, on_complete),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲，保证实时推送
        },
    )


__all__ = [
    "sse_event",
    "sse_chat_stream",
    "chunked_text_stream",
]