"""probe_ping 原语守底（sentinel 业务轮）：推理模型兼容判定 + no_cache 旁路 + 零重试。

背景：deepseek-flash（推理模型）在 max_tokens=8 下 content 恒空（reasoning 吃光
预算，HTTP 200 正常返回），旧 _standard_llm_check 用 ask() 把它判 fail——nexus-agent
探针假阳性 10h+；ironman cache_mw 又把空响应缓存回放，探针永不触达真实链路。
probe_ping 三点差异 = 推理兼容 / no_cache / 零重试。
"""
import pytest

import nexus.llm_probes as llm_mod
from nexus.llm import LLMService


class _Resp:
    def __init__(self, content: str = "", reasoning: str | None = None, model: str = "deepseek-flash") -> None:
        self.content = content
        self.reasoning = reasoning
        self.model = model
        self.usage = None
        self.latency_ms = 1.0
        self.cached = False
        self.provider = "test"


@pytest.fixture(autouse=True)
def _no_ironman_setup(monkeypatch):
    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(llm_mod, "configure_ironman", _noop)
    monkeypatch.setattr(llm_mod, "_preflight", _noop)


def _patch_chat(monkeypatch, resp: _Resp, captured: dict) -> None:
    async def fake_chat(messages: list, llm: object = None, tools: object = None) -> _Resp:
        captured["llm"] = llm
        captured["messages"] = messages
        return resp

    monkeypatch.setattr("ironman.chat", fake_chat)


@pytest.mark.asyncio
async def test_reasoning_only_reply_is_ok(monkeypatch) -> None:
    """推理模型 content 空 + reasoning 非空 = 链路活着（探针不再假阳性）。"""
    captured: dict = {}
    _patch_chat(monkeypatch, _Resp(content="", reasoning="  用户ping了一下，我回 pong 即可  "), captured)
    out = await LLMService().probe_ping()
    assert out["ok"] is True
    assert out["reasoning_only"] is True
    assert "ping" in out["reply_head"]


@pytest.mark.asyncio
async def test_no_cache_flag_reaches_options(monkeypatch) -> None:
    captured: dict = {}
    _patch_chat(monkeypatch, _Resp(content="pong"), captured)
    out = await LLMService().probe_ping()
    assert out["ok"] is True
    assert out["reasoning_only"] is False
    assert captured["llm"].no_cache is True
    assert captured["llm"].max_tokens == 16
    assert captured["messages"][0].content == "ping"


@pytest.mark.asyncio
async def test_both_empty_is_fail(monkeypatch) -> None:
    """content 与 reasoning 双空才是真 fail（canonical 错误向上透传）。"""
    _patch_chat(monkeypatch, _Resp(content="", reasoning=None), {})
    out = await LLMService().probe_ping()
    assert out["ok"] is False
    assert "LLMEmptyContentError" in out["error"]


@pytest.mark.asyncio
async def test_zero_retries_single_provider_call(monkeypatch) -> None:
    calls = {"n": 0}

    async def flaky_chat(messages: list, llm: object = None, tools: object = None) -> _Resp:
        calls["n"] += 1
        raise RuntimeError("provider down")

    monkeypatch.setattr("ironman.chat", flaky_chat)
    out = await LLMService().probe_ping()
    assert out["ok"] is False
    assert "provider down" in out["error"]
    assert calls["n"] == 1, "探针重试会放大故障期流量并拖长探测延迟"


def test_standard_llm_check_judgments(monkeypatch) -> None:
    """_standard_llm_check 三态映射：reasoning-only→ok(带语义 detail)/error→fail/正常→ok。"""
    import asyncio

    import nexus.business_probe as bp

    class _Svc:
        def __init__(self, reply: dict) -> None:
            self._reply = reply

        async def probe_ping(self, timeout: float = 20.0) -> dict:
            return self._reply

    async def _ironman_ok() -> bool:
        return True

    monkeypatch.setattr("nexus.ironman.is_ironman_available", _ironman_ok)

    import nexus
    monkeypatch.setattr(nexus, "get_llm_service", lambda: _Svc({
        "ok": True, "reply_head": "我在想", "reasoning_only": True,
        "latency_ms": 812, "model": "deepseek-flash", "error": "",
    }))
    got = asyncio.run(bp._standard_llm_check())
    assert got["status"] == "ok"
    assert "reasoning-only" in got["detail"]

    monkeypatch.setattr(nexus, "get_llm_service", lambda: _Svc({
        "ok": False, "reply_head": "", "reasoning_only": False,
        "latency_ms": 2, "model": "", "error": "LLMTimeoutError: timeout",
    }))
    got = asyncio.run(bp._standard_llm_check())
    assert got["status"] == "fail"
    assert "LLMTimeoutError" in got["detail"]
