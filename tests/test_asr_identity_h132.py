"""h132 asr_transcribe 出站身份盖章守底：网关直连链必须带 X-App-Id+trace 头。

匿名桶根治（全舰队 ASR 面自 11-01 起 42/42 匿名）：撤守必红两态——
headers 丢身份盖章即红；丢 trace 头即红。
"""
from __future__ import annotations

import pytest

from nexus import asr


class _FakeResp:
    status_code = 200

    def json(self):
        return {"text": "hello", "duration": 0.5}


class _FakeClient:
    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = {"url": url, "headers": headers}
        return _FakeResp()


@pytest.mark.asyncio
async def test_asr_transcribe_stamps_identity_and_trace(monkeypatch):
    import ironman.bootstrap as bs

    async def fake_cfg():
        return {"base_url": "http://gw.test", "api_key": "k"}

    monkeypatch.setattr(bs, "current_app_name", lambda: "Memoria", raising=False)
    monkeypatch.setattr(asr, "get_promptmanager_config", fake_cfg)
    monkeypatch.setattr(asr.httpx, "AsyncClient", _FakeClient)

    result = await asr.asr_transcribe(audio_data="aGVsbG8=", format="wav")
    assert result.get("success") is not False
    headers = _FakeClient.captured["headers"]
    assert headers.get("X-App-Id") == "Memoria"
    assert headers.get("X-Request-ID")
    assert headers.get("Authorization") == "Bearer k"


@pytest.mark.asyncio
async def test_asr_transcribe_unbootstrapped_still_sends(monkeypatch):
    """未引导进程：身份头缺席但请求照发（存量行为零变化锚）。"""

    async def fake_cfg():
        return {"base_url": "http://gw.test", "api_key": "k"}

    monkeypatch.setattr(asr, "get_promptmanager_config", fake_cfg)
    monkeypatch.setattr(asr.httpx, "AsyncClient", _FakeClient)

    result = await asr.asr_transcribe(audio_data="aGVsbG8=", format="wav")
    assert result.get("success") is not False
    assert "X-App-Id" not in _FakeClient.captured["headers"]
