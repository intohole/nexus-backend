"""SSE 流式响应助手 — 统一对话流式输出协议。

所有 app 的 chat_stream 端点应使用 `sse_chat_stream` / `sse_response` 包装异步生成器，
避免每个 app 各写一套 SSE 协议。

事件格式（遵循 SSE 规范）：
    data: {"event": "delta", "content": "..."}\n\n
    data: {"event": "done", "content": "完整文本"}\n\n
    data: {"event": "error", "error": "..."}\n\n

统一事件 schema（推荐用 sse_event_dict）：
    data: {"type": "delta", "content": "..."}\n\n
    data: {"type": "done", "content": "完整文本"}\n\n
    data: {"type": "error", "message": "..."}\n\n

若 ironman 暂不支持原生 streaming，调用方可先用 `chunked_text_stream`
将完整文本切块后 yield，模拟流式 UX。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Union

from nexus.logging import get_logger

logger = get_logger("nexus.streaming")


SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_event(event: str, data: Optional[dict[str, Any]] = None) -> str:
    """格式化单个 SSE 事件（旧 schema：payload 含 event 字段）。"""
    payload = {"event": event}
    if data:
        payload.update(data)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_event_dict(event_type: str, payload: Optional[dict[str, Any]] = None) -> str:
    """格式化单个 SSE 事件（新 schema：统一 type 字段）。

    统一约定：所有事件必须带 type 字段，取值如
    start / delta / content / thinking / tool_executed / references /
    queue / queue_ready / done / error。
    error 事件统一为 {"type":"error", "message": "..."}。
    """
    data: dict[str, Any] = {"type": event_type}
    if payload:
        data.update(payload)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(
    generator: AsyncIterator[str],
    media_type: str = "text/event-stream",
):
    """将异步生成器包装为 StreamingResponse，统一注入 SSE_HEADERS。

    消除各端点重复写 `StreamingResponse(gen, media_type=..., headers={...})` 样板。
    """
    from fastapi.responses import StreamingResponse

    return StreamingResponse(generator, media_type=media_type, headers=SSE_HEADERS)


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
    return sse_response(_sse_generator(chat_fn, on_complete))


async def with_disconnect_check(
    request: Optional[Any],
    chat_fn: AsyncIterator[str],
) -> AsyncIterator[str]:
    """包装 chat_fn，客户端断连时立即停止迭代。

    消除各端点重复实现 `await request.is_disconnected()` 检测逻辑。
    request 为 None 时直接透传 chat_fn。
    """
    if request is None:
        async for chunk in chat_fn:
            yield chunk
        return
    async for chunk in chat_fn:
        try:
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping stream")
                return
        except Exception as exc:
            logger.warning("disconnect check failed: %s", exc)
        yield chunk


async def _sse_generator_v2(
    chat_fn: AsyncIterator[str],
    request: Optional[Any] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
) -> AsyncIterator[str]:
    """增强版 SSE 生成器：支持断连检测 + 事件回调。

    chat_fn 可 yield str（当作 delta）或 dict（必须含 type 字段）。
    """
    accumulated: list[str] = []
    try:
        async for chunk in with_disconnect_check(request, chat_fn):
            if not chunk:
                continue
            if isinstance(chunk, dict):
                event_type: str = chunk.get("type", "delta")
                if event_type == "delta" and "content" in chunk:
                    accumulated.append(str(chunk["content"]))
                if on_event:
                    try:
                        await on_event(event_type, chunk)
                    except Exception as exc:
                        logger.warning("on_event callback failed: %s", exc)
                yield sse_event_dict(event_type, {k: v for k, v in chunk.items() if k != "type"})
            else:
                accumulated.append(str(chunk))
                if on_event:
                    try:
                        await on_event("delta", {"content": str(chunk)})
                    except Exception as exc:
                        logger.warning("on_event callback failed: %s", exc)
                yield sse_event_dict("delta", {"content": str(chunk)})
        full_content = "".join(accumulated)
        if on_complete:
            try:
                await on_complete(full_content)
            except Exception as exc:
                logger.warning("on_complete callback failed: %s", exc)
        yield sse_event_dict("done", {"content": full_content})
    except Exception as exc:
        logger.error("SSE stream error: %s", exc)
        yield sse_event_dict("error", {"message": str(exc)})


def sse_chat_stream_v2(
    chat_fn: AsyncIterator[Union[str, dict[str, Any]]],
    request: Optional[Any] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
):
    """增强版 sse_chat_stream：支持断连检测 + 多事件类型 + 事件回调。

    chat_fn 可 yield：
        str  -> 自动包装为 {"type":"delta","content":chunk}
        dict -> 必须含 type 字段，其余字段作为 payload

    用法：
        async def my_stream(msg: str) -> AsyncIterator[dict]:
            yield {"type": "thinking", "content": "正在思考..."}
            async for chunk in llm.stream(msg):
                yield chunk  # str
            yield {"type": "references", "items": [...]}
        return sse_chat_stream_v2(my_stream(msg), request=request)
    """
    return sse_response(_sse_generator_v2(chat_fn, request, on_complete, on_event))


async def queue_wait_stream(
    queue_item: Any,
    queue_manager: Any,
    poll_interval: float = 2.0,
    slot_timeout: float = 0.5,
) -> AsyncIterator[str]:
    """统一的 LLM 排队等待 SSE 流，yield queue/queue_ready/error 事件。

    消除 WisePath 4 个端点重复的排队等待逻辑。返回前需配合 sse_response 使用。

    用法：
        async def gen():
            async for event in queue_wait_stream(item, q):
                yield event
            if queue_item.cancelled:
                return
            async for chunk in llm_stream():
                yield sse_event_dict("delta", {"content": chunk})
            yield sse_event_dict("done")
        return sse_response(gen())
    """
    if not getattr(queue_item, "ready_event", None) or queue_item.ready_event.is_set():
        yield sse_event_dict("queue_ready")
        return

    while not queue_item.ready_event.is_set() and not getattr(queue_item, "cancelled", False):
        position = queue_manager.get_position(queue_item)
        if position > 0:
            est_wait = queue_manager.get_estimated_wait(position)
            yield sse_event_dict("queue", {
                "position": position,
                "estimated_wait": round(est_wait),
            })
        await asyncio.sleep(poll_interval)

    if getattr(queue_item, "cancelled", False):
        yield sse_event_dict("error", {"message": "排队超时，请稍后再试"})
        return

    got_slot = await queue_manager.wait_for_slot(queue_item, timeout=slot_timeout)
    if not got_slot:
        yield sse_event_dict("error", {"message": "排队超时，请稍后再试"})
        return

    yield sse_event_dict("queue_ready")


__all__ = [
    "SSE_HEADERS",
    "sse_event",
    "sse_event_dict",
    "sse_response",
    "sse_chat_stream",
    "sse_chat_stream_v2",
    "chunked_text_stream",
    "with_disconnect_check",
    "queue_wait_stream",
]