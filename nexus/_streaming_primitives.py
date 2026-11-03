"""SSE 协议原语层：事件格式化/响应包装的零依赖底座（streaming facade 分层的第三层）。

依赖单向：本模块 ← _streaming_sse ← streaming（facade）；本模块不 import 任何 nexus 兄弟模块。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# OpenAI 兼容协议的流结束哨兵帧（promptManager 网关 / geniusStudent 等 OpenAI 形协议使用）
SSE_DONE: str = "data: [DONE]\n\n"


def sse_event_dict(event_type: str, payload: Optional[dict[str, Any]] = None) -> str:
    """格式化单个 SSE 事件（新 schema：统一 type 字段）。

    统一约定：所有事件必须带 type 字段，取值如
    start / delta / content / thinking / tool_executed / references /
    widget / widget_update / queue / queue_ready / done / error。
    error 事件统一为 {"type":"error", "message": "..."}。
    """
    data: dict[str, Any] = {"type": event_type}
    if payload:
        data.update(payload)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_data_line(
    payload: dict[str, Any],
    event: Optional[str] = None,
    **json_kwargs: Any,
) -> str:
    """将字典序列化为单条 data 行 SSE 事件。

    适用于已有 type 字段的 payload（如 {type: "done", ...}），
    与 sse_event_dict 的区别是不再注入 type 字段。
    event 传入时在 data 行前追加 `event: <name>` 行（SSE 事件名协议，
    前端按 event 字段路由，如 adSmart generate-stream）。
    json_kwargs 透传 json.dumps（如 default=str 序列化 datetime/Decimal）。
    """
    data_line = f"data: {json.dumps(payload, ensure_ascii=False, **json_kwargs)}\n\n"
    if event:
        return f"event: {event}\n{data_line}"
    return data_line


def sse_raw_frame(text: str) -> str:
    """将已序列化的文本包装为 data 帧（如上游透传的 JSON 字符串、lion watch 推送）。"""
    return f"data: {text}\n\n"


def sse_response(
    generator: AsyncIterator[str],
    media_type: str = "text/event-stream",
):
    """将异步生成器包装为 StreamingResponse，统一注入 SSE_HEADERS。

    消除各端点重复写 `StreamingResponse(gen, media_type=..., headers={...})` 样板。
    """
    from fastapi.responses import StreamingResponse

    return StreamingResponse(generator, media_type=media_type, headers=SSE_HEADERS)
