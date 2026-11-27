"""r263 刀1：summary_llm_from_gateway 适配构件守底（入库不接线，激活=B1 首接触发器）。

三态映射实锚：finish_reason∈{length,content_filter}→SummaryCallOutcome（ironman
summarize() 据此拒落盘截断摘要，pi compaction.ts:590 纪律）；其余（含缓存命中
finish_reason=""）恒 str 返回零破坏。身份门与 gateway_executor 同律：构造即复核。
"""

import pytest
from ironman.memory.summarizer import SummaryCallOutcome

from nexus.harness_llm import HarnessGatewayError, summary_llm_from_gateway


def _gateway_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.ironman.is_ironman_available", lambda: True)
    monkeypatch.setattr("nexus.ironman_config.is_gateway_mode", lambda: True)
    monkeypatch.setattr("ironman.bootstrap.current_app_name", lambda: "demo")


def _fake_service(monkeypatch: pytest.MonkeyPatch, finish_reason: str, content: str = "摘要文本") -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    async def fake_chat_with_usage(self, messages, **kwargs):
        seen.append(dict(messages[0]))
        return {
            "content": content,
            "model": "fake",
            "total_tokens": 1,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 1,
            "provider": "fake",
            "cached": False,
            "finish_reason": finish_reason,
        }

    monkeypatch.setattr("nexus.llm.LLMService.chat_with_usage", fake_chat_with_usage)
    return seen


def test_construct_requires_gateway_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.ironman.is_ironman_available", lambda: True)
    monkeypatch.setattr("nexus.ironman_config.is_gateway_mode", lambda: False)
    with pytest.raises(HarnessGatewayError):
        summary_llm_from_gateway("demo")


@pytest.mark.asyncio
async def test_length_maps_to_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway_ready(monkeypatch)
    _fake_service(monkeypatch, "length", content="截断摘要")
    outcome = await summary_llm_from_gateway("demo")("总结这段对话")
    assert isinstance(outcome, SummaryCallOutcome)
    assert outcome.finish_reason == "length"
    assert outcome.text == "截断摘要"


@pytest.mark.asyncio
async def test_content_filter_maps_to_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway_ready(monkeypatch)
    _fake_service(monkeypatch, "content_filter")
    outcome = await summary_llm_from_gateway("demo")("总结这段对话")
    assert isinstance(outcome, SummaryCallOutcome)
    assert outcome.finish_reason == "content_filter"


@pytest.mark.asyncio
async def test_plain_and_cached_return_str(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway_ready(monkeypatch)
    _fake_service(monkeypatch, "stop")
    text = await summary_llm_from_gateway("demo")("总结这段对话")
    assert text == "摘要文本"
    _fake_service(monkeypatch, "", content="缓存命中摘要")
    text = await summary_llm_from_gateway("demo")("总结这段对话")
    assert text == "缓存命中摘要"


@pytest.mark.asyncio
async def test_prompt_flows_to_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway_ready(monkeypatch)
    seen = _fake_service(monkeypatch, "stop")
    await summary_llm_from_gateway("demo")("总结这段对话")
    assert seen and seen[0]["role"] == "user" and seen[0]["content"] == "总结这段对话"


@pytest.mark.asyncio
async def test_per_call_route_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """r263 走查 P3-5：构造后网关模式翻转→调用期身份门仍拦截（与 gateway_executor 同律）。"""
    _gateway_ready(monkeypatch)
    _fake_service(monkeypatch, "stop")
    summarize = summary_llm_from_gateway("demo")
    monkeypatch.setattr("nexus.ironman_config.is_gateway_mode", lambda: False)
    with pytest.raises(HarnessGatewayError):
        await summarize("总结这段对话")


@pytest.mark.asyncio
async def test_gateway_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """r263 走查 P3-5：网关异常直接上抛——由 ironman summarize() 的 except 分支
    收敛为 degraded fallback（合规路径，构件自身不吞异常）。"""

    async def boom(self, messages, **kwargs):
        raise RuntimeError("gateway down")

    _gateway_ready(monkeypatch)
    monkeypatch.setattr("nexus.llm.LLMService.chat_with_usage", boom)
    with pytest.raises(RuntimeError, match="gateway down"):
        await summary_llm_from_gateway("demo")("总结这段对话")
