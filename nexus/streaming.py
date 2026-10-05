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

消息内组件事件（组件协议，前端 nux-ai-chat 自动路由渲染）：
    data: {"type": "widget", "id": "w1", "widget": "table", "title": "技术指标",
           "data": {"summary": {"信号": "买入"}, "columns": ["指标", "数值"], "rows": [...]}}\n\n
    data: {"type": "widget_update", "id": "w1", "data": {...}}\n\n
    widget 取值: table(数据表) / cards(卡片列表) / steps(步骤时间线, 支持 percent+status 呈现任务进度) /
                 related(相关追问, data.items[{text,value}], 点击自动追问) /
                 choice(选项选择, data.options[{label,value,recommended}]) /
                 feedback(赞踩反馈) /
                 form(表单收集, data.fields[{key,label,type,required,options}], 提交回传字段值) /
                 chart(图表, data.chart=bar|line|pie + categories/series, 或直接传 echarts option) /
                 confirm(危险操作二次确认, data.message/detail/items/warning, 回传 true|false)
    widget_update 用于流式填充或更新已下发组件的 data（按 id 定位）；task 进度用
    {"type":"widget_update","id":"w1","data":{"percent":60,"status":"running","steps":[...]}}。
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

# OpenAI 兼容协议的流结束哨兵帧（promptManager 网关 / geniusStudent 等 OpenAI 形协议使用）
SSE_DONE: str = "data: [DONE]\n\n"

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _openai_sse_payloads(raw: str):
    """逐帧产出 OpenAI 兼容 SSE 文本中的已解析 JSON 载荷（全文回读变体的共用游标）。"""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def openai_sse_content(raw: str) -> str:
    """解析 OpenAI 兼容 SSE 全文，拼接全部 content 增量（openai_sse_deltas 的同步全文版）。

    遍历所有 choices（n>1 时 output token 口径需涵盖全部）；用于拿到完整响应
    后回读统计/落库的场景；坏帧静默跳过。
    """
    parts: list[str] = []
    for chunk in _openai_sse_payloads(raw):
        for choice in chunk.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(content)
    return "".join(parts)


def openai_sse_usage(raw: str) -> Optional[dict[str, Any]]:
    """提取 OpenAI 兼容 SSE 的 usage 帧（stream_options.include_usage 的末帧）。

    返回首个非空 usage dict，无则 None；坏帧静默跳过。
    """
    for chunk in _openai_sse_payloads(raw):
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            return usage
    return None


async def openai_sse_deltas(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """解析 OpenAI 兼容 SSE 行流，产出 content 增量（客户端侧，与 emit 侧 sse_* 对偶）。

    lines 为按行迭代的异步流（如 httpx resp.aiter_lines()）；提取 data: 帧
    delta.content，[DONE] 哨兵终止，坏帧静默跳过。
    """
    async for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue
        content = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if content:
            yield content


class ThinkStreamFilter:
    """流式 <think> 标签过滤器：增量喂入 content，输出剥离思维链后的文本。

    跨标签边界的部分标签会暂存，直到可判定后输出；流结束时调用 flush()。
    """

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._inside:
                idx = self._buf.find(THINK_CLOSE)
                if idx == -1:
                    keep = len(THINK_CLOSE) - 1
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:]
                    break
                self._inside = False
                self._buf = self._buf[idx + len(THINK_CLOSE):]
            else:
                o = self._buf.find(THINK_OPEN)
                c = self._buf.find(THINK_CLOSE)
                if o != -1 and (c == -1 or o < c):
                    if o > 0:
                        out.append(self._buf[:o])
                    self._inside = True
                    self._buf = self._buf[o + len(THINK_OPEN):]
                elif c != -1:
                    if c > 0:
                        out.append(self._buf[:c])
                    self._buf = self._buf[c + len(THINK_CLOSE):]
                else:
                    keep = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1
                    if len(self._buf) > keep:
                        out.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    break
        return "".join(out)

    def flush(self) -> str:
        if self._inside:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


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


_STREAM_SENTINEL = object()

# 有界队列默认容量：慢客户端时上游读取在此暂停（TCP 背压回传 LLM provider），
# 内存占用有上限；0 表示无界（旧语义，不推荐）。
DEFAULT_SSE_QUEUE_SIZE = 256


async def _sse_generator_v2(
    chat_fn: AsyncIterator[Union[str, dict[str, Any]]],
    request: Optional[Any] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
    heartbeat_interval: float = 0.0,
    queue_size: int = DEFAULT_SSE_QUEUE_SIZE,
) -> AsyncIterator[str]:
    """SSE 生成器：断连检测 + 事件回调 + 心跳 + done 去重 + 有界队列背压。

    chat_fn 可 yield str（当作 delta）或 dict（必须含 type 字段）。
    heartbeat_interval > 0 时，超过该秒数无新事件则下发 {"type":"heartbeat"} 保活；
    业务流自身已发过 done 时不重复补发 done。
    queue_size > 0 时队列有界：消费慢于生产时 _pump 阻塞在 put 上，向上游施加背压。
    """
    accumulated: list[str] = []
    try:
        ait = with_disconnect_check(request, chat_fn).__aiter__()
        done_sent = False
        queue: asyncio.Queue = asyncio.Queue(maxsize=max(0, queue_size))

        async def _pump() -> None:
            try:
                async for item in ait:
                    await queue.put(item)
            except Exception as exc:
                await queue.put(exc)
            finally:
                # 被取消（消费者已终止）时跳过哨兵投递：满队列上的阻塞 put
                # 无人唤醒会让 pump_task 永久挂起，连带挂死生成器关闭路径。
                task = asyncio.current_task()
                cancelling = getattr(task, "cancelling", int)() if task else 0
                if cancelling == 0:
                    await queue.put(_STREAM_SENTINEL)

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval or None
                    )
                except asyncio.TimeoutError:
                    yield sse_event_dict("heartbeat")
                    continue
                if chunk is _STREAM_SENTINEL:
                    break
                if isinstance(chunk, BaseException):
                    raise chunk
                if not chunk:
                    continue
                if isinstance(chunk, dict):
                    event_type: str = chunk.get("type", "delta")
                    if event_type == "delta" and "content" in chunk:
                        accumulated.append(str(chunk["content"]))
                    if event_type == "done":
                        done_sent = True
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
        finally:
            if not pump_task.done():
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass
        full_content = "".join(accumulated)
        if on_complete:
            try:
                await on_complete(full_content)
            except Exception as exc:
                logger.warning("on_complete callback failed: %s", exc)
        if not done_sent:
            yield sse_event_dict("done", {"content": full_content})
    except Exception as exc:
        logger.error("SSE stream error: %s", exc)
        yield sse_event_dict("error", {"message": str(exc)})


def sse_chat_stream_v2(
    chat_fn: AsyncIterator[Union[str, dict[str, Any]]],
    request: Optional[Any] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
    heartbeat_interval: float = 0.0,
    queue_size: int = DEFAULT_SSE_QUEUE_SIZE,
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

    heartbeat_interval: 超过该秒数无新事件时下发 heartbeat 保活（0 关闭）。
    queue_size: 事件队列容量上限（默认 256）——消费慢于生产时向上游施加背压，
        防止慢客户端把整条 LLM 流缓冲进内存；0 恢复无界旧语义。
    """
    return sse_response(
        _sse_generator_v2(
            chat_fn, request, on_complete, on_event, heartbeat_interval, queue_size
        )
    )


__all__ = [
    "SSE_HEADERS",
    "SSE_DONE",
    "DEFAULT_SSE_QUEUE_SIZE",
    "sse_event_dict",
    "sse_data_line",
    "sse_raw_frame",
    "sse_response",
    "sse_chat_stream_v2",
    "ThinkStreamFilter",
    "openai_sse_deltas",
    "openai_sse_content",
    "openai_sse_usage",
]