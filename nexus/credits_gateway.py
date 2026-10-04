"""LLM 网关自动计费域：kind→feature 映射 + 档位定价扣费 + 调用前预检。

作为 CreditsService 的 mixin（nexus.credits 组合），llm.py 四路出口经
get_credits_service().auto_charge_llm / gateway_preflight 调用。
"""
from __future__ import annotations

from nexus.config import yaml_bool, yaml_get
from nexus.credits_tiers import resolve_model_tier, tier_label
from nexus.logging import get_logger

logger = get_logger("nexus.credits")

# LLM 网关自动计费：kind → 计费 feature 映射（embed 是基础设施型调用，默认不计费）
GATEWAY_KIND_FEATURE: dict[str, str] = {
    "chat": "chat",
    "chat_stream": "chat",
    "extract": "extract",
    "embed": "embed",
}
GATEWAY_KIND_DESC: dict[str, str] = {
    "chat": "AI 对话",
    "chat_stream": "AI 对话",
    "extract": "AI 抽取",
    "embed": "AI 向量",
}
DEFAULT_AUTO_CHARGE_KINDS = "chat,chat_stream,extract"


class GatewayBillingMixin:
    async def auto_charge_llm(
        self, *, kind: str, app_name: str, request_id: str, model: str = ""
    ) -> None:
        """LLM 网关出口自动计费：调用成功后按平台价×模型档位系数扣积分，故障静默放行。

        ref_id 幂等防 HTTP 重试双扣；显式动作计费的应用用 credits.auto_charge=false 关停。
        """
        feature = self.gateway_feature(kind)
        if not feature:
            return
        _, factor = resolve_model_tier(model)
        description = GATEWAY_KIND_DESC.get(kind, "")
        if factor != 1.0:
            description = f"{description}·{tier_label(model)}"
        try:
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
            if result.reason == "overdraft_limit":
                from nexus.credits import CreditsInsufficientError

                raise CreditsInsufficientError(
                    "免费体验额度已用完，每日赠送积分到账后可继续使用"
                )
            from nexus.credits import CreditsInsufficientError

            raise CreditsInsufficientError("积分余额不足，请充值后重试")

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
