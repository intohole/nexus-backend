"""SSE v2 流水线机制：断连检测 + 事件回调 + 心跳 + done 去重 + 有界队列背压 + drain 兜底。

从 streaming.py 拆出（r158 模块级拆分，代码逐字节搬运零行为变更）；
logger 沿用 "nexus.streaming" 名（S47 可观测登记身份不变）。
依赖单向：_streaming_primitives ← 本模块 ← streaming（facade）。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Union

from nexus._streaming_primitives import sse_event_dict, sse_response
from nexus.logging import get_logger

logger = get_logger("nexus.streaming")

_STREAM_SENTINEL = object()

# 有界队列默认容量：慢客户端时上游读取在此暂停（TCP 背压回传 LLM provider），
# 内存占用有上限；0 表示无界（旧语义，不推荐）。
DEFAULT_SSE_QUEUE_SIZE = 256

# 断连后 drain 的总预算秒数：LLM 长流的最长正常时长即可，超过视为上游挂死，
# 放弃 drain 并取消 pump（防孤儿 task 泄漏）。
DRAIN_TIMEOUT_SECONDS = 300.0

# drain task 的模块级强引用：asyncio 只持弱引用，无引用的运行中 task 会被 GC
# 中途回收（官方文档 asyncio.create_task 警告），drain 又恰在客户端断开后运行，
# 没人 await 它——必须自持引用，结束（含异常/取消）后自摘。
_DRAIN_TASKS: set[asyncio.Task] = set()


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


async def _drain_after_disconnect(
    queue: "asyncio.Queue",
    accumulated: list[str],
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    """客户端断开后的兜底消费者：接手 queue，把业务流消费到自然结束。

    生产实锤（aiPet r14）：用户在首页连点快捷提问，每次点击都放弃上一个流，
    LLM 已完整生成并计费，但业务生成器尾部的落库随流被取消——回复凭空丢失。
    原则：回复是资产、连接只是通道；通道可断，资产必须落地。
    drain 接手的是 queue 而非业务生成器——pump 仍是 chat_fn 的唯一消费者，
    队列有 drain 这个读者，pump 的背压 put 才不会永久阻塞。
    """
    try:
        while True:
            item = await queue.get()
            if item is _STREAM_SENTINEL:
                break
            if isinstance(item, BaseException):
                break
            if not item:
                continue
            if isinstance(item, dict):
                if item.get("type") == "delta" and "content" in item:
                    accumulated.append(str(item["content"]))
                if on_event:
                    try:
                        await on_event(str(item.get("type", "delta")), item)
                    except Exception:
                        pass
            else:
                accumulated.append(str(item))
                if on_event:
                    try:
                        await on_event("delta", {"content": str(item)})
                    except Exception:
                        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if on_complete:
            try:
                await on_complete("".join(accumulated))
            except Exception:
                pass
        _DRAIN_TASKS.discard(asyncio.current_task())


def _classify_sse_chunk(
    chunk: Union[str, dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any], Optional[str]]:
    """归一一条上游 chunk：(事件类型, on_event 载荷, 下发载荷, 待累积增量)。

    dict 事件 on_event 收原始 chunk（含 type 键），下发载荷剥 type；
    str 增量统一视为 delta，两载荷同形。增量为 None 表示非 delta 事件不累积。
    """
    if isinstance(chunk, dict):
        event_type: str = chunk.get("type", "delta")
        yield_payload = {k: v for k, v in chunk.items() if k != "type"}
        delta = str(chunk["content"]) if event_type == "delta" and "content" in chunk else None
        return event_type, chunk, yield_payload, delta
    content = str(chunk)
    return "delta", {"content": content}, {"content": content}, content


async def _teardown_pump(
    pump_task: "asyncio.Task",
    queue: "asyncio.Queue",
    accumulated: list[str],
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]],
    on_complete: Optional[Callable[[str], Awaitable[None]]],
    drain_on_disconnect: bool,
) -> None:
    """生成器关闭时 pump 任务拆卸：客户端断开移交 drain 兜底消费者（业务流
    含尾部落库跑到自然结束，总闸防上游挂死），否则取消收割防悬死。"""
    if pump_task.done():
        return
    if drain_on_disconnect:
        async def _guarded_drain() -> None:
            try:
                await asyncio.wait_for(
                    _drain_after_disconnect(queue, accumulated, on_event, on_complete),
                    timeout=DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("SSE drain timed out after %.0fs", DRAIN_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("SSE drain failed: %s", exc)
            finally:
                if not pump_task.done():
                    pump_task.cancel()

        drain_task = asyncio.create_task(_guarded_drain())
        _DRAIN_TASKS.add(drain_task)
        drain_task.add_done_callback(_DRAIN_TASKS.discard)
    else:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass


async def _sse_generator_v2(
    chat_fn: AsyncIterator[Union[str, dict[str, Any]]],
    request: Optional[Any] = None,
    on_complete: Optional[Callable[[str], Awaitable[None]]] = None,
    on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
    heartbeat_interval: float = 0.0,
    queue_size: int = DEFAULT_SSE_QUEUE_SIZE,
    drain_on_disconnect: bool = False,
) -> AsyncIterator[str]:
    """SSE 生成器：断连检测 + 事件回调 + 心跳 + done 去重 + 有界队列背压。

    chat_fn 可 yield str（当作 delta）或 dict（必须含 type 字段）。
    heartbeat_interval > 0 时，超过该秒数无新事件则下发 {"type":"heartbeat"} 保活；
    业务流自身已发过 done 时不重复补发 done。
    queue_size > 0 时队列有界：消费慢于生产时 _pump 阻塞在 put 上，向上游施加背压。
    drain_on_disconnect: 客户端断开时不再取消业务流，移交独立 drain task 消费到
        自然结束（业务生成器尾部的落库/回调照常执行，on_complete 在 drain 尾触发），
        总预算 DRAIN_TIMEOUT_SECONDS 秒。默认 False 保持旧语义（断开即取消）。
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
                event_type, notify_payload, yield_payload, delta = _classify_sse_chunk(chunk)
                if delta is not None:
                    accumulated.append(delta)
                if event_type == "done":
                    done_sent = True
                if on_event:
                    try:
                        await on_event(event_type, notify_payload)
                    except Exception as exc:
                        logger.warning("on_event callback failed: %s", exc)
                yield sse_event_dict(event_type, yield_payload)
        finally:
            await _teardown_pump(
                pump_task, queue, accumulated, on_event, on_complete, drain_on_disconnect
            )
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
    drain_on_disconnect: bool = False,
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
    drain_on_disconnect: 客户端断开时移交 drain 兜底消费者，业务流（含尾部
        落库）跑到自然结束，on_complete 在 drain 尾触发。适用于「回复是资产」
        的场景（LLM 已计费、落库在生成器尾部）；默认 False 保持断开即取消。
    """
    return sse_response(
        _sse_generator_v2(
            chat_fn, request, on_complete, on_event, heartbeat_interval, queue_size,
            drain_on_disconnect=drain_on_disconnect,
        )
    )
