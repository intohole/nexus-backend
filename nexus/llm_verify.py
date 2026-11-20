"""LLM 结构化输出验证回路：schema 校验失败带错误回喂的有界修复环。

r128 收敛：修复轮吃 r234 finish_reason 分型语义（length→放大预算+no_cache；
其他→同预算 no_cache 绕缓存自愈）。chat_fn 返回 bill dict（svc.chat
_want_usage=True 形态）时读 finish_reason 驱动条件化；返回 str 时维持
原行为（repair_no_cache 缺省 False，零行为变化）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from nexus.llm_json import is_llm_json_error, parse_llm_json

logger = logging.getLogger("nexus.llm_verify")

# 输出预算修复放大系数（r234 同语义单源：llm_client 反向 import 本处常量）
REPAIR_BUDGET_MIN = 8000
REPAIR_BUDGET_MAX = 16000

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


def _repair_call_kwargs(
    call_kwargs: dict[str, object], finish_reason: str, repair_no_cache: bool
) -> dict[str, object]:
    """修复轮参数构造：length→预算放大（r234 同系数），其余同预算；no_cache 按 repair_no_cache。"""
    repair = dict(call_kwargs)
    if finish_reason == "length":
        budget = repair.get("max_tokens")
        if budget is None:
            bigger = REPAIR_BUDGET_MAX
        else:
            bigger = min(REPAIR_BUDGET_MAX, max(int(budget) * 3, REPAIR_BUDGET_MIN))
        if budget is None or bigger > int(budget):
            repair["max_tokens"] = bigger
    if repair_no_cache:
        repair["no_cache"] = True
    return repair


async def verified_json_chat(
    chat_fn: Callable[..., Awaitable[object]],
    messages: list[dict[str, object]],
    *,
    schema: type[BaseModel],
    repair_attempts: int = 1,
    raise_on_error: bool = True,
    repair_no_cache: bool = False,
    **call_kwargs: object,
) -> dict[str, object]:
    convo = list(messages)

    async def _call(kw: dict[str, object]) -> tuple[str, str]:
        raw = await chat_fn(messages=convo, json_mode=True, **kw)
        if isinstance(raw, dict):
            return str(raw.get("content") or ""), str(raw.get("finish_reason") or "")
        return str(raw), ""

    raw, finish_reason = await _call(call_kwargs)
    parsed = parse_llm_json(raw)
    err = _judge(parsed, schema)
    attempts = 0
    while err is not None and attempts < repair_attempts:
        attempts += 1
        logger.warning("LLM 输出未通过 %s 校验（第 %s 次修复，finish_reason=%s）：%s", schema.__name__, attempts, finish_reason or "unknown", err)
        convo.append({"role": "assistant", "content": raw})
        convo.append(
            {
                "role": "user",
                "content": _REPAIR_HINT.format(err=err, raw=raw[:_RAW_FEEDBACK_LIMIT]),
            }
        )
        raw, finish_reason = await _call(_repair_call_kwargs(call_kwargs, finish_reason, repair_no_cache))
        parsed = parse_llm_json(raw)
        err = _judge(parsed, schema)
    if err is None:
        assert isinstance(parsed, dict)
        return parsed
    if raise_on_error:
        raise ValueError(f"LLM 输出未通过 {schema.__name__} 校验（含 {attempts} 次修复）：{err}")
    return {"raw_response": str(raw)}
