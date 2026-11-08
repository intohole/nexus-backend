from __future__ import annotations

import httpx
import pytest

from nexus.web_search_providers import SearchProvider, SearchConfig, ProviderSearchResponse


class _ProbeProvider(SearchProvider):
    @property
    def name(self) -> str:
        return "probe"

    async def _do_search(self, query: str, config: SearchConfig) -> ProviderSearchResponse:
        return ProviderSearchResponse()


@pytest.mark.asyncio
async def test_identity_header_injected_when_app_declared(monkeypatch) -> None:
    import ironman.bootstrap as bs

    monkeypatch.setattr(bs, "current_app_name", lambda: "mountain", raising=False)
    provider = _ProbeProvider(api_key="k", base_url="http://gw/api/gateway/v1")
    assert await provider._ensure_client() is True
    assert provider._client.headers.get("X-App-Id") == "mountain"
    await provider.close()


@pytest.mark.asyncio
async def test_identity_header_absent_when_no_app(monkeypatch) -> None:
    import ironman.bootstrap as bs

    def _raise() -> str:
        raise RuntimeError("ironman not initialized")

    monkeypatch.setattr(bs, "current_app_name", _raise, raising=False)
    provider = _ProbeProvider(api_key="k")
    assert await provider._ensure_client() is True
    assert "X-App-Id" not in provider._client.headers
    await provider.close()


@pytest.mark.asyncio
async def test_identity_header_refreshed_on_reused_client(monkeypatch) -> None:
    import ironman.bootstrap as bs

    monkeypatch.setattr(bs, "current_app_name", lambda: "", raising=False)
    provider = _ProbeProvider(api_key="k")
    await provider._ensure_client()
    assert "X-App-Id" not in provider._client.headers
    monkeypatch.setattr(bs, "current_app_name", lambda: "golden", raising=False)
    await provider._ensure_client()
    assert provider._client.headers.get("X-App-Id") == "golden"
    await provider.close()
