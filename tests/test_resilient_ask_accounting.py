"""resilient_ask 记账口径守底：无 usage 回传时整笔记 completion（保守侧）+model 留空。

背景：2026-11-25 DSH×中间件差距评测 P1-差距5——ask 主路径曾按 estimated_tokens//2
硬拆+model=alias 记账（cost_guard docstring 自认 //2 已废弃；alias 非定价键触发
unknown fallback warning 刷屏）。修复=对齐 call_with_guard 降级链同款口径。
"""
from __future__ import annotations

import pytest

import nexus.resilient_llm as rl


class _FakeService:
    async def ask(self, prompt: str, **kwargs: object) -> str:
        return "ok"


class _SpyGuard:
    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    async def check_budget(self, estimated_tokens: int = 0) -> dict[str, object]:
        return {"allowed": True}

    async def record_usage(self, **kwargs: object) -> None:
        self.recorded.append(kwargs)


@pytest.mark.asyncio
async def test_ask_records_conservative_split_without_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyGuard()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: spy)

    result = await rl.resilient_ask(
        "hello", alias="mygroup", operation="intent", estimated_tokens=800, retry_count=0
    )

    assert result == "ok"
    assert len(spy.recorded) == 1
    rec = spy.recorded[0]
    assert rec["prompt_tokens"] == 0
    assert rec["completion_tokens"] == 800
    assert rec["model"] == ""
    assert rec["operation"] == "intent"


@pytest.mark.asyncio
async def test_ask_failure_does_not_record_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomService:
        async def ask(self, prompt: str, **kwargs: object) -> str:
            raise RuntimeError("boom")

    spy = _SpyGuard()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _BoomService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: spy)

    with pytest.raises(RuntimeError):
        await rl.resilient_ask("hello", alias="g", retry_count=0)

    assert spy.recorded == []
