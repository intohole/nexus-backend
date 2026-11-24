"""UC SDK 计费混入：平台积分钱包接口分组（独立成模块，沿用 uc_sdk 分层先例）。"""
from typing import Dict


class BillingMixin:
    """平台积分钱包：计费消费走 X-Service-Token（服务端代表用户扣费），查询类走用户 Bearer。"""

    async def _service_headers(self) -> Dict[str, str]:
        token = await self._ensure_service_token()
        return {"X-Service-Token": token} if token else {}

    async def billing_consume(self, user_id: int, app_key: str, feature: str,
                              ref_id: str, description: str = None,
                              cost_factor: float = 1.0) -> dict:
        return await self._request("POST", "/api/billing/consume", json={
            "user_id": user_id, "app": app_key, "feature": feature,
            "ref_id": ref_id, "description": description, "cost_factor": cost_factor,
        }, headers=await self._service_headers())

    async def billing_consume_usage(self, user_id: int, app_key: str, feature: str,
                                    ref_id: str, model: str, input_tokens: int,
                                    output_tokens: int, cached_tokens: int,
                                    tier: str = "standard", description: str = None,
                                    called_at: str = None) -> dict:
        return await self._request("POST", "/api/billing/consume-usage", json={
            "user_id": user_id, "app": app_key, "feature": feature, "ref_id": ref_id,
            "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_tokens": cached_tokens, "tier": tier, "description": description,
            "called_at": called_at,
        }, headers=await self._service_headers())

    async def billing_report_meters(self, items: list) -> dict:
        return await self._request("POST", "/api/billing/meters",
                                   json={"items": items}, headers=await self._service_headers())

    async def billing_precheck(self, user_id: int, app_key: str, feature: str,
                               cost_factor: float = 1.0) -> dict:
        return await self._request(
            "GET",
            f"/api/billing/precheck?app={app_key}&feature={feature}"
            f"&user_id={user_id}&cost_factor={cost_factor}",
            headers=await self._service_headers(),
        )
