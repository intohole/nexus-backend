from __future__ import annotations

from typing import Optional

from nexus.logging import get_logger
from nexus.llm_optimizer import CONCISENESS_HINT, JSON_ONLY_HINT
from nexus.llm_budget import OutputMode, PROSE_HINT

logger = get_logger("nexus.llm_helpers")


def apply_output_discipline(
    system: Optional[str],
    prompt: str,
    concise: bool,
    json_mode: bool,
    output_mode: Optional[OutputMode] = None,
) -> tuple[Optional[str], str]:
    if output_mode is None:
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
    if getattr(response, "reasoning", None):
        logger.warning(
            "LLM returned empty content, using reasoning as fallback [req_id=%s, tokens=%s]",
            request_id,
            getattr(response.usage, "completion_tokens", "?"),
        )
        return response.reasoning
    logger.warning("LLM returned empty content and no reasoning [req_id=%s]", request_id)
    return ""


def record_usage(
    metrics: object,
    app_name: str,
    response: object,
    latency: float,
    error: Optional[str],
) -> None:
    usage = getattr(response, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    model: str = getattr(response, "model", "") or "unknown"
    cached: bool = bool(getattr(response, "cached", False))
    cost_usd: float = float(getattr(response, "cost_usd", 0.0) or 0.0)
    metrics.record(
        app_name,
        model,
        latency,
        tokens=tokens,
        error=error,
        cached=cached,
        cost_usd=cost_usd,
    )