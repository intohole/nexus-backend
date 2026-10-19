"""类型安全转换与数据整形：safe 系转换、区间钳制、列表分页、JSON 落库归一化。"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import Any, TypeVar

T = TypeVar("T")


def loads_or(raw: str | bytes | None, default: T) -> T:
    """JSON 列/配置字段安全解析：空值、脏 JSON、JSON null 一律回 default。

    与 llm_utils.parse_llm_json_or 的分工：本函数面向程序写入的存储字段，
    只做严格 loads，不做 LLM 输出的代码块剥离与片段提取。
    """
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return default if value is None else value


def clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    return min(max(value, min_val), max_val)


def safe_float(value: object, default: float = 0.0, min_val: float = -float("inf"), max_val: float = float("inf")) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    if result != result:
        return default
    return clamp(result, min_val, max_val)


def safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def jsonable(value: Any) -> Any:
    """递归收敛为 JSON 安全类型：Decimal→float、date/datetime→isoformat、set/tuple→list。

    JSON 列落库走裸 json.dumps（不走 FastAPI 的 jsonable_encoder），
    NUMERIC 列直出的 Decimal 混进 dict 就是落库 TypeError——写 JSON 列前先过本函数。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def paginate_from_skip(items: list[object], skip: int, limit: int) -> dict[str, object]:
    """从skip/limit参数生成分页响应"""
    total: int = len(items)
    page_items: list[object] = items[skip:skip + limit]
    return {
        "items": page_items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_more": skip + limit < total,
    }
