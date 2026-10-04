"""模型档位解析：模型名 → (档位, 价格系数)，供 LLM 网关自动计费按档定价。

档位语义（映射真实成本差异，免费期即建立分档心智，付费期定价直接沿用）：
- lite 轻量模型 ×0.5      mini/flash/lite/haiku/turbo/7b/8b 等（glm-4-flash/qwen-turbo）
- premium 旗舰模型 ×2.0   gpt-4/o1/opus/deepseek-r1/qwen-max/glm-4.5 等
- standard 默认 ×1.0      未知模型与未命中规则
lite 先于 premium 匹配：家族名后缀的轻量标记（glm-4.5-flash）优先于家族名（glm-4.5）。

规则可用项目配置覆盖：credits.tier_premium_models / credits.tier_lite_models
（逗号分隔子串，大小写不敏感），credits.tier_premium_factor / credits.tier_lite_factor。
"""
from __future__ import annotations

from nexus.config import yaml_get

DEFAULT_PREMIUM_MODELS = (
    "gpt-4,o1,o3,opus,deepseek-r1,deepseek-reasoner,qwen-max,qwen3-max,"
    "glm-4.5,glm-4.6,max,ultra,grand"
)
DEFAULT_LITE_MODELS = (
    "mini,flash,lite,haiku,turbo,8b,7b,4b,1.5b,nano,small,air"
)
DEFAULT_PREMIUM_FACTOR = 2.0
DEFAULT_LITE_FACTOR = 0.5

_cache: dict[str, tuple[str, float]] | None = None
_cache_key: tuple[str, str, float, float] | None = None


def _patterns(raw: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def _config() -> tuple[tuple[str, ...], tuple[str, ...], float, float]:
    premium_raw = str(yaml_get("credits", "tier_premium_models", DEFAULT_PREMIUM_MODELS))
    lite_raw = str(yaml_get("credits", "tier_lite_models", DEFAULT_LITE_MODELS))
    try:
        premium_factor = float(yaml_get("credits", "tier_premium_factor", DEFAULT_PREMIUM_FACTOR))
    except (TypeError, ValueError):
        premium_factor = DEFAULT_PREMIUM_FACTOR
    try:
        lite_factor = float(yaml_get("credits", "tier_lite_factor", DEFAULT_LITE_FACTOR))
    except (TypeError, ValueError):
        lite_factor = DEFAULT_LITE_FACTOR
    return _patterns(premium_raw), _patterns(lite_raw), premium_factor, lite_factor


def resolve_model_tier(model: str) -> tuple[str, float]:
    """模型名 → (档位名, 价格系数)；空名/未知 → standard 1.0。规则结果进程内缓存。"""
    global _cache, _cache_key
    name = str(model or "").strip().lower()
    if not name:
        return ("standard", 1.0)
    cfg = _config()
    if _cache is None or _cache_key != cfg:
        _cache = {}
        _cache_key = cfg
        for tier, patterns, factor in (
            ("lite", cfg[1], cfg[3]),
            ("premium", cfg[0], cfg[2]),
        ):
            for pattern in patterns:
                _cache[pattern] = (tier, factor)
    for pattern, hit in _cache.items():
        if pattern in name:
            return hit
    return ("standard", 1.0)


def tier_label(model: str) -> str:
    tier, _ = resolve_model_tier(model)
    return {"premium": "旗舰模型", "lite": "轻量模型"}.get(tier, "标准模型")
