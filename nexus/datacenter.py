from __future__ import annotations

import os
from typing import Optional

import httpx

from nexus.logging import get_logger
from nexus.utils import HttpClient
from nexus.infra import get_uc_base_url

logger = get_logger("nexus.datacenter")

DOMAIN_CAREER = 1
DOMAIN_KNOWLEDGE = 2
DOMAIN_CREATIVE = 3
DOMAIN_GROWTH = 4
DOMAIN_ASSET = 5


class DatacenterClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._base_url: str = base_url or os.environ.get(
            "UC_BASE_URL", "http://localhost:8901"
        )
        self._timeout: float = timeout
        self._http: HttpClient = HttpClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )

    def _bearer(self, user_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {user_token}"}

    async def report(
        self,
        user_token: str,
        domain: int,
        asset_type: str,
        app: str,
        ref_id: str,
        title: str,
        summary: Optional[str] = None,
        payload: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "domain": domain,
            "asset_type": asset_type,
            "app": app,
            "ref_id": ref_id,
            "title": title,
            "summary": summary,
            "payload": payload,
        }
        try:
            resp: httpx.Response = await self._http.post(
                "/api/datacenter/assets",
                json=body,
                headers=self._bearer(user_token),
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Datacenter report failed: status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return {}
        except Exception as exc:
            logger.error("Datacenter report error: %s", str(exc))
            return {}

    async def list_assets(
        self,
        user_token: str,
        domain: Optional[int] = None,
        asset_type: str = "",
        app: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, object]:
        params: dict[str, object] = {"page": page, "page_size": page_size}
        if domain is not None:
            params["domain"] = domain
        if asset_type:
            params["asset_type"] = asset_type
        if app:
            params["app"] = app
        try:
            resp: httpx.Response = await self._http.get(
                "/api/datacenter/assets",
                params=params,
                headers=self._bearer(user_token),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("Datacenter list failed: %s", str(exc))
            return {}

    async def delete(
        self,
        user_token: str,
        asset_id: int,
    ) -> bool:
        try:
            resp: httpx.Response = await self._http.delete(
                f"/api/datacenter/assets/{asset_id}",
                headers=self._bearer(user_token),
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Datacenter delete failed: %s", str(exc))
            return False


async def report_core(
    user_token: str,
    items: list[dict[str, object]],
) -> dict[str, int]:
    """批量幂等上报核心产出条目，供各应用\"同步我的产出\"复用。

    items: [{domain, asset_type, app, ref_id, title, summary?}]
    """
    if not user_token or not items:
        return {"requested": 0, "succeeded": 0, "new": 0}
    client: DatacenterClient = await get_datacenter_client()
    succeeded: int = 0
    new_count: int = 0
    for item in items:
        result: dict[str, object] = await client.report(
            user_token,
            domain=int(item["domain"]),
            asset_type=str(item["asset_type"]),
            app=str(item["app"]),
            ref_id=str(item["ref_id"]),
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or "") if item.get("summary") else None,
        )
        if result:
            succeeded += 1
            info: dict[str, object] | None = result.get("data")
            if isinstance(info, dict) and info.get("created"):
                new_count += 1
    return {"requested": len(items), "succeeded": succeeded, "new": new_count}


_aggregated_client: Optional[DatacenterClient] = None


async def get_datacenter_client() -> DatacenterClient:
    global _aggregated_client
    if _aggregated_client is None:
        base_url = await get_uc_base_url()
        _aggregated_client = DatacenterClient(base_url=base_url)
    return _aggregated_client