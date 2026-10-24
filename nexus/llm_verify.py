"""LLM 结构化输出验证回路：schema 校验失败带错误回喂的有界修复环。"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from nexus.llm import get_llm_service
from nexus.llm_json import is_llm_json_error, parse_llm_json

logger = logging.getLogger("nexus.llm_verify")

_REPAIR_HINT = (
    "你的上一次输出未通过结构校验，错误信息：{err}。"
    "上次输出：{raw}\n"
    "请只输出修正后的合法 JSON，不要解释，不要输出 JSON 以外的内容。"
)
_RAW_FEEDBACK_LIMIT = 2000
_ERROR_DETAIL_LIMIT = 3


def _judge(parsed: object, schema: type[BaseModel]) -> Optional[str]:
    if is_llm_json_error(parsed):
        return "输出不是合法 JSON"
    if not isinstance(parsed, dict):
        return f"输出顶层必须是 JSON 对象，实际为 {type(parsed).__name__}"
    try:
        schema.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'root'}: {err['msg']}"
            for err in exc.errors()[:_ERROR_DETAIL_LIMIT]
        )
        return f"不符合 {schema.__name__} 结构（{details}）"
    return None


async def verified_json_chat(
    chat_fn: Callable[..., Awaitable[object]],
    messages: list[dict[str, str]],
    *,
    schema: type[BaseModel],
    repair_attempts: int = 1,
    raise_on_error: bool = True,
    **call_kwargs: object,
) -> dict[str, object]:
    convo = list(messages)
    raw = await chat_fn(messages=convo, json_mode=True, **call_kwargs)
    parsed = parse_llm_json(str(raw))
    err = _judge(parsed, schema)
    attempts = 0
    while err is not None and attempts < repair_attempts:
        attempts += 1
        logger.warning("LLM 输出未通过 %s 校验（第 %s 次修复）：%s", schema.__name__, attempts, err)
        convo.append({"role": "assistant", "content": str(raw)})
        convo.append(
            {
                "role": "user",
                "content": _REPAIR_HINT.format(err=err, raw=str(raw)[:_RAW_FEEDBACK_LIMIT]),
            }
        )
        raw = await chat_fn(messages=convo, json_mode=True, **call_kwargs)
        parsed = parse_llm_json(str(raw))
        err = _judge(parsed, schema)
    if err is None:
        assert isinstance(parsed, dict)
        return parsed
    if raise_on_error:
        raise ValueError(f"LLM 输出未通过 {schema.__name__} 校验（含 {attempts} 次修复）：{err}")
    return {"raw_response": str(raw)}


async def ask_json_verified(
    prompt: str,
    system: Optional[str] = None,
    *,
    schema: type[BaseModel],
    repair_attempts: int = 1,
    raise_on_error: bool = True,
    **kwargs: object,
) -> dict[str, object]:
    svc = get_llm_service()
    kwargs.setdefault("temperature", 0.2)
    kwargs.setdefault("max_tokens", 1500)
    return await verified_json_chat(
        svc.chat,
        [{"role": "user", "content": prompt}],
        schema=schema,
        repair_attempts=repair_attempts,
        raise_on_error=raise_on_error,
        system=system,
        **kwargs,
    )


async def chat_json_verified(
    messages: list[dict[str, str]],
    system: Optional[str] = None,
    *,
    schema: type[BaseModel],
    repair_attempts: int = 1,
    raise_on_error: bool = True,
    **kwargs: object,
) -> dict[str, object]:
    svc = get_llm_service()
    kwargs.setdefault("temperature", 0.2)
    kwargs.setdefault("max_tokens", 1500)
    return await verified_json_chat(
        svc.chat,
        messages,
        schema=schema,
        repair_attempts=repair_attempts,
        raise_on_error=raise_on_error,
        system=system,
        **kwargs,
    )
