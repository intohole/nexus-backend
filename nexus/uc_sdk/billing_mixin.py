"""UC SDK 计费混入：平台积分钱包接口分组（独立成模块，沿用 uc_sdk 分层先例）。"""
from typing import Dict


class BillingMixin:
    """平台积分钱包：计费消费走 X-Service-Token（服务端代表用户扣费），查询类走用户 Bearer。"""

    async def _service_headers(self) -> Dict[str, str]:
        token = await self._ensure_service_token()
        return {"X-Service-Token": token} if token else {}

    async def billing_consume(self, user_id: int, app_key: str, feature: str,
                              ref_id: str, description: str = None) -> dict:
        return await self._request("POST", "/api/billing/consume", json={
            "user_id": user_id, "app": app_key, "feature": feature,
            "ref_id": ref_id, "description": description,
        }, headers=await self._service_headers())

    async def billing_quote(self, app_key: str, feature: str) -> dict:
        return await self._request(
            "GET", f"/api/billing/quote?app={app_key}&feature={feature}",
            headers=await self._service_headers(),
        )

    async def billing_catalog(self, token: str = None, app_key: str = None) -> dict:
        path = "/api/billing/catalog"
        if app_key:
            path += f"?app={app_key}"
        return await self._request("GET", path, token=token)

    async def billing_wallet_summary(self, token: str) -> dict:
        return await self._request("GET", "/api/points/summary", token=token)

    async def billing_transactions(self, token: str, direction: str = "all",
                                   page: int = 1, page_size: int = 20) -> dict:
        return await self._request(
            "GET",
            f"/api/points/transactions?direction={direction}&page={page}&page_size={page_size}",
            token=token,
        )

    async def billing_consumptions(self, token: str, page: int = 1, page_size: int = 20) -> dict:
        return await self._request(
            "GET", f"/api/billing/consumptions?page={page}&page_size={page_size}", token=token,
        )

    async def billing_meters_summary(self, token: str) -> dict:
        return await self._request("GET", "/api/billing/meters/summary", token=token)

    async def billing_report_meters(self, items: list) -> dict:
        return await self._request("POST", "/api/billing/meters",
                                   json={"items": items}, headers=await self._service_headers())
