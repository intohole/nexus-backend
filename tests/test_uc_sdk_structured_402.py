"""uc_sdk 4xx 响应解析：UC 结构化 402（积分引导）的 message 提取与 error_data 透传。"""
import httpx
import pytest

from nexus.uc_sdk.client import UserCenterSDK


def _sdk_with_response(status_code: int, payload: dict) -> UserCenterSDK:
    sdk = UserCenterSDK(base_url="http://uc-test", app_key="app_test", app_secret="s")
    request = httpx.Request("POST", "http://uc-test/api/billing/consume")

    class _FakeClient:
        async def request(self, method, url, headers=None, **kwargs):
            return httpx.Response(status_code, json=payload, request=request)

    async def _get_client():
        return _FakeClient()

    sdk._get_client = _get_client
    sdk._access_token = "t"
    sdk._circuit_breaker._failure_count = 0
    return sdk


@pytest.mark.asyncio
async def test_402_structured_detail_extracts_message_and_error_data():
    sdk = _sdk_with_response(402, {"detail": {
        "message": "免费体验额度已用完（体验授信 30/50）",
        "code": "overdraft_limit", "balance": 0,
        "overdraft": {"used": 30, "limit": 50},
        "wallet_url": "/user-center.html#wallet",
    }})
    res = await sdk._request("POST", "/api/billing/consume", json={})
    assert res["success"] is False
    assert res["message"] == "免费体验额度已用完（体验授信 30/50）"
    assert "额度已用完" in res["message"]
    assert res["error_data"]["code"] == "overdraft_limit"
    assert res["error_data"]["overdraft"] == {"used": 30, "limit": 50}


@pytest.mark.asyncio
async def test_402_plain_detail_keeps_string_message():
    sdk = _sdk_with_response(402, {"detail": "积分余额不足，请先充值"})
    res = await sdk._request("POST", "/api/billing/consume", json={})
    assert res["success"] is False
    assert res["message"] == "积分余额不足，请先充值"
    assert "error_data" not in res
