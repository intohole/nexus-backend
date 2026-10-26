"""LLM 调用辅助：输出纪律注入、前缀纪律装配、命名空间解析、消息转换与用量记账。"""
from __future__ import annotations

from typing import AsyncGenerator, Iterable, Optional

from nexus.logging import get_logger
from nexus.llm_optimizer import CONCISENESS_HINT, JSON_ONLY_HINT
from nexus.llm_budget import OutputMode, PROSE_HINT
from nexus.llm_utils import LLMEmptyContentError

logger = get_logger("nexus.llm_helpers")


def with_tail_context(user_content: str, *blocks: str) -> str:
    """前缀纪律原语：动态上下文块后置本轮用户消息尾部，system 前缀保持逐字节稳定。

    deepseek 缓存从首 token 精确匹配前缀（命中价差 46-100×），system 嵌时间戳/
    检索结果/用户记忆等每请求变化的内容会让该点之后全部击穿；多轮会话下更是
    连历史轮次缓存一并丢失。范式源：nexus-agent `_build_system/_build_tail_context`。
    """
    tail = "\n\n".join(block.strip() for block in blocks if block and block.strip())
    if not tail:
        return user_content
    return f"{user_content}\n\n{tail}" if user_content else tail


def disciplined_messages(
    system: str,
    history: Iterable[dict[str, str]],
    user_content: str,
) -> list[dict[str, str]]:
    """前缀纪律消息装配：[稳定 system] + 历史透传 + 本轮用户消息。

    与 with_tail_context 配套：动态内容一律先进尾部再传入 user_content，
    本函数不改写 history（保证跨请求消息前缀逐字节稳定）。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for msg in history:
        role = msg.get("role", "")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": msg.get("content", "")})
    messages.append({"role": "user", "content": user_content})
    return messages


def apply_output_discipline(
    system: Optional[str],
    prompt: str,
    concise: bool,
    json_mode: bool,
    output_mode: Optional[OutputMode] = None,
) -> tuple[Optional[str], str]:
    if output_mode is None or output_mode == OutputMode.DEFAULT:
        if not concise:
            return system, prompt
        hint = JSON_ONLY_HINT if json_mode else CONCISENESS_HINT
    elif output_mode == OutputMode.CONCISE:
        hint = CONCISENESS_HINT
    elif output_mode == OutputMode.JSON:
        hint = JSON_ONLY_HINT
    elif output_mode == OutputMode.PROSE:
        hint = PROSE_HINT
    else:
        return system, prompt
    if system:
        return (system + "\n\n" + hint), prompt
    return hint, prompt


def resolve_namespace(namespace: Optional[str]) -> Optional[str]:
    if namespace:
        return namespace
    from nexus.context import get_user_id as _get_user_id
    return _get_user_id() or None


def convert_messages(
    messages: list[dict[str, str]],
    system: Optional[str],
) -> list:
    from ironman.types import Message, Role

    ironman_messages: list = []
    if system:
        ironman_messages.append(Message(role=Role.SYSTEM, content=system))
    for msg in messages:
        role_str = msg.get("role", "user")
        content = msg.get("content", "")
        if role_str == "user":
            ironman_messages.append(Message(role=Role.USER, content=content))
        elif role_str == "assistant":
            ironman_messages.append(Message(role=Role.ASSISTANT, content=content))
        elif role_str == "system":
            ironman_messages.append(Message(role=Role.SYSTEM, content=content))
    return ironman_messages


def extract_content(response: object, request_id: str = "-") -> str:
    if response.content:
        return response.content
    reasoning = getattr(response, "reasoning", None) or ""
    raise LLMEmptyContentError(
        f"LLM returned empty content (reasoning_chars={len(reasoning)}) [req_id={request_id}]"
    )


def record_usage(
    metrics: object,
    app_name: str,
    response: object,
    latency: float,
    error: Optional[str],
) -> None:
    usage = getattr(response, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    cached_tokens = int(getattr(usage, "cached_tokens", 0) or 0)
    model: str = getattr(response, "model", "") or "unknown"
    cost_usd: float = float(getattr(response, "cost_usd", 0.0) or 0.0)
    metrics.record(
        app_name,
        model,
        latency,
        tokens=tokens,
        error=error,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
    )

async def stream_chunks(
    chat_stream,
    msgs: list,
    llm_opts: object,
) -> AsyncGenerator[str, None]:
    """流式调用统一引擎：think 过滤、空内容重试与用量记账。

    chat_stream 为 ironman.chat_stream；msgs/llm_opts 由调用方按
    convert_messages + LLMOptions 组装。
    """
    import time
    from typing import Optional as _Optional

    from nexus.context import get_request_id
    from nexus.llm_config import resolve_app_name
    from nexus.llm_metrics import get_llm_metrics
    from nexus.streaming import ThinkStreamFilter

    metrics = get_llm_metrics()
    app_name: str = resolve_app_name()
    request_id: str = get_request_id() or "-"
    start: float = time.monotonic()
    has_content: bool = False
    think_filter = ThinkStreamFilter()
    last_usage: _Optional[object] = None
    last_model: str = "unknown"
    max_attempts: int = 2
    for attempt in range(max_attempts):
        async for chunk in chat_stream(messages=msgs, llm=llm_opts):
            if chunk.content:
                piece = think_filter.feed(chunk.content)
                if piece:
                    has_content = True
                    yield piece
            if chunk.usage is not None:
                last_usage = chunk.usage
            if chunk.model:
                last_model = chunk.model
        tail = think_filter.flush()
        if tail:
            has_content = True
            yield tail
        if has_content:
            break
        if attempt + 1 < max_attempts:
            think_filter = ThinkStreamFilter()
            logger.warning(f"stream_chat: empty visible content (attempt {attempt + 1}), retrying [req_id=%s]", request_id)
    metrics.record(
        app_name,
        last_model,
        time.monotonic() - start,
        tokens=int(getattr(last_usage, "total_tokens", 0) or 0),
        error=None,
    )
    if not has_content:
        logger.warning("stream_chat: no visible content after think-filter [req_id=%s]", request_id)
        yield "抱歉，本次未能生成有效回答，请换个问法或稍后重试。"
