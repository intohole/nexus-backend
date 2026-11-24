"""LLM 网关自动计费域：kind→feature 映射 + 按量/档位定价扣费 + 调用前预检。

作为 CreditsService 的 mixin（nexus.credits 组合），llm.py 四路出口经
get_credits_service().auto_charge_llm / gateway_preflight 调用。

按量通道（usage billing）：调用方传 usage 原料（模型返回 tokens/缓存命中）时走
UC consume_usage（真实成本折算积分，缓存命中便宜/峰谷贵随成本传导）；usage 缺失
或 tokens 全零（估算口径/上游未回 usage）退化为按次档位计价，绝不因按量失据而漏计。
"""
from __future__ import annotations

from datetime import datetime, timezone

from nexus.config import yaml_bool, yaml_get
from nexus.credits_tiers import resolve_model_tier, tier_label
from nexus.logging import get_logger

logger = get_logger("nexus.credits")

# LLM 网关自动计费：kind → 计费 feature 映射（embed 是基础设施型调用，默认不计费；
# vision 由 ironman 插桩层按请求特征判定后以 kind="vision" 进入）
GATEWAY_KIND_FEATURE: dict[str, str] = {
    "chat": "chat",
    "chat_stream": "chat",
    "extract": "extract",
    "embed": "embed",
    "vision": "vision",
}
GATEWAY_KIND_DESC: dict[str, str] = {
    "chat": "AI 对话",
    "chat_stream": "AI 对话",
    "extract": "AI 抽取",
    "embed": "AI 向量",
    "vision": "AI 视觉",
}
DEFAULT_AUTO_CHARGE_KINDS = "chat,chat_stream,extract,vision"


class GatewayBillingMixin:
    @staticmethod
    def _usable_usage(usage: dict | None) -> dict | None:
        """usage 原料可用性判定：tokens 有真实读数才走按量（估算口径退按次）。"""
        if not isinstance(usage, dict):
            return None
        if int(usage.get("input_tokens") or 0) <= 0 and int(usage.get("output_tokens") or 0) <= 0:
            return None
        return usage

    async def auto_charge_llm(
        self, *, kind: str, app_name: str, request_id: str, model: str = "",
        usage: dict | None = None,
    ) -> None:
        """LLM 网关出口自动计费：usage 原料在则按量（UC 折算真实成本），否则按档位按次。

        ref_id 幂等防 HTTP 重试双扣；显式动作计费的应用用 credits.auto_charge=false 关停。
        """
        feature = self.gateway_feature(kind)
        if not feature:
            return
        tier, factor = resolve_model_tier(model)
        description = GATEWAY_KIND_DESC.get(kind, "")
        if factor != 1.0:
            description = f"{description}·{tier_label(model)}"
        real_usage = self._usable_usage(usage)
        try:
            if real_usage is not None:
                outcome = await self.consume_usage(
                    feature,
                    {**real_usage, "model": str(real_usage.get("model") or model),
                     "called_at": real_usage.get("called_at")
                     or datetime.now(timezone.utc).isoformat()},
                    ref_id=f"llm:{request_id}:{self._next_charge_seq()}"[:100],
                    description=description,
                    app_key=app_name,
                    tier=tier,
                )
            else:
                outcome = await self.consume(
                    feature,
                    ref_id=f"llm:{request_id}:{self._next_charge_seq()}"[:100],
                    description=description,
                    app_key=app_name,
                    cost_factor=factor,
                )
            if not outcome.allowed:
                logger.warning(
                    "LLM 网关计费余额不足(trial 透支/formal 已由前置预检拦截): kind=%s app=%s",
                    kind, app_name,
                )
        except Exception as exc:
            logger.warning("LLM 网关计费失败(放行): %s", exc)

    async def gateway_preflight(self, kind: str, model: str = "") -> None:
        """计费模式下 LLM 调用前预检，余额不足/体验授信用尽抛 CreditsInsufficientError（402）。

        缓存的 charge_mode 为 trial 或 formal 时发起预检——trial 拦的是体验授信超限
        （fair-use），formal 拦的是余额不足；off 与缓存冷启动零开销直通。
        异常携带结构化 details（balance/cost/charge_mode/wallet_url）供应用侧引导卡消费。
        """
        mode = self._cached_charge_mode()
        if mode not in ("trial", "formal"):
            return
        feature = self.gateway_feature(kind)
        if not feature:
            return
        _, factor = resolve_model_tier(model)
        result = await self.precheck(feature, cost_factor=factor)
        if not result.allowed:
            from nexus.credits import CreditsInsufficientError

            raw = result.raw or {}
            details: dict = {
                "code": result.reason,
                "balance": result.balance,
                "cost": result.cost,
                "charge_mode": result.charge_mode or mode,
                "wallet_url": "/user-center.html#wallet",
            }
            for key in ("recovery", "overdraft", "lite_grace", "grace", "list_cost"):
                if key in raw:
                    details[key] = raw[key]
            if result.reason == "overdraft_limit":
                raise CreditsInsufficientError(
                    "免费体验额度已用完，每日赠送积分到账后可继续使用",
                    code="OVERDRAFT_LIMIT",
                    details=details,
                )
            raise CreditsInsufficientError(
                "积分余额不足，请充值后重试",
                code="INSUFFICIENT_CREDITS",
                details=details,
            )

    @classmethod
    def gateway_feature(cls, kind: str) -> str:
        """LLM kind → 计费 feature；网关计费关停（credits.auto_charge=false）返回空。"""
        feature = GATEWAY_KIND_FEATURE.get(kind, "")
        if not feature:
            return ""
        if not yaml_bool("credits", "auto_charge", True):
            return ""
        if kind not in cls._auto_charge_kinds():
            return ""
        return feature

    @staticmethod
    def _auto_charge_kinds() -> set[str]:
        raw = yaml_get("credits", "auto_charge_kinds", DEFAULT_AUTO_CHARGE_KINDS)
        return {k.strip() for k in raw.split(",") if k.strip()}
