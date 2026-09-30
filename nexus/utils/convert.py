"""类型安全转换与数据整形：safe 系转换、区间钳制、列表分页。"""
from __future__ import annotations


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
