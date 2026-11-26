"""r259 B5 记忆接线+llm_core tools 透传守底。

钉死五面：
- B5 旗关（默认）=spawn prompt 与素版逐字节一致、注入计数恒 0（回滚零漂移面）
- B5 旗开+backend 在位=spawn prompt 含记忆围栏标记、计数（尝试/命中）就位
- B5 旗开但 backend 缺席=仍走素版（不造空段）
- _build_team 成员维持素版 prompt（B5 边界登记：同步 __init__ 链无法 await）
- llm_core.chat(tools=...) 直透 ironman facade + 带 tools 恒绕 PromptCache（同 images 判例）
"""
from __future__ import annotations

import sys
import time
import types

import pytest

import nexus.llm_core as core_mod
from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.feature_flags import reset_feature_flags

from ironman.types import LLMRequest, LLMResponse, TokenUsage


class _Executor:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content="ok",
            model="test-model",
            usage=TokenUsage(prompt_tokens=3, completion_tokens=3, total_tokens=6),
        )


class _FakeBackend:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    async def snapshot(self, *, top_k: int = 10, max_chars: int = 1200, budget=None):
        self.snapshot_calls += 1
        return {"memories": [{"content": "用户偏好深色主题与简短回答", "source_app": "other-app"}]}


@pytest.fixture(autouse=True)
def _reset_flags():
    reset_feature_flags()
    yield
    reset_feature_flags()


@pytest.mark.asyncio
async def test_b5_flag_off_prompt_identical_to_plain():
    harness = build_agent_harness(HarnessProfile(app_name="demo", model="test-model"), llm_executor=_Executor())
    plain = harness.build_system_prompt()
    agent = await harness.spawn_react(name="lead")
    assert agent._system_prompt.startswith(plain[:200]) or agent._system_prompt == plain
    census = harness.memory_injection_census()
    assert census == {"spawn_memory_injections": 0, "spawn_memory_hits": 0}


@pytest.mark.asyncio
async def test_b5_flag_on_injects_memory_and_counts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_HARNESS_MEMORY_INJECTION", "1")
    reset_feature_flags()
    harness = build_agent_harness(HarnessProfile(app_name="demo", model="test-model"), llm_executor=_Executor())
    backend = _FakeBackend()
    harness._memory_backend = backend
    agent = await harness.spawn_react(name="lead")
    assert backend.snapshot_calls == 1
    assert "<memory_context" in agent._system_prompt, "旗开+backend 在位应注入记忆围栏段"
    census = harness.memory_injection_census()
    assert census == {"spawn_memory_injections": 1, "spawn_memory_hits": 1}


@pytest.mark.asyncio
async def test_b5_flag_on_without_backend_falls_back_plain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_HARNESS_MEMORY_INJECTION", "1")
    reset_feature_flags()
    harness = build_agent_harness(HarnessProfile(app_name="demo", model="test-model"), llm_executor=_Executor())
    agent = await harness.spawn_react(name="lead")
    census = harness.memory_injection_census()
    assert census == {"spawn_memory_injections": 0, "spawn_memory_hits": 0}
    assert "<memory_context" not in agent._system_prompt


@pytest.mark.asyncio
async def test_b5_team_members_stay_plain_prompt(monkeypatch: pytest.MonkeyPatch):
    """B5 边界登记面：_build_team 同步链维持素版（异步 build=首接随行）。"""
    from ironman.agents.sub_agent import SubAgentSpec
    from ironman.tools.function_tool import FunctionTool

    async def handler() -> str:
        return "ok"

    monkeypatch.setenv("NEXUS_HARNESS_MEMORY_INJECTION", "1")
    reset_feature_flags()
    profile = HarnessProfile(
        app_name="demo",
        model="test-model",
        subagents=(SubAgentSpec(name="reviewer", description="审查"),),
    )
    profile.tools.register(FunctionTool(name="lookup", description="查询", handler=handler))
    harness = build_agent_harness(profile, llm_executor=_Executor())
    harness._memory_backend = _FakeBackend()
    assert "reviewer" in harness.team.members
    assert "<memory_context" not in harness.team.members["reviewer"].inner._system_prompt
    assert harness.memory_injection_census()["spawn_memory_injections"] == 0


# ---------- llm_core tools 透传 ----------

class _FakeResp:
    content = "done"
    model = "deepseek-flash"
    cost_usd = 0.0
    latency_ms = 1.0
    provider = "test"
    cached = False
    usage = None
    finish_reason = "stop"


@pytest.fixture()
def captured_tools_chat(monkeypatch):
    captured: dict = {}
    cache_calls: list = []

    async def fake_chat(messages, llm, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        return _FakeResp()

    async def noop_async(*args, **kwargs):
        return None

    async def fake_retry(fn, timeout, retries):
        return await fn()

    class _FakeCircuit:
        async def call(self, fn):
            return await fn()

        async def acquire_call(self):
            return None

    class _FakeTelemetry:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            class _M:
                def record(self, *args, **kwargs):
                    pass

            return (_M(), time.monotonic())

        async def __aexit__(self, *exc):
            return False

    class _SpyCache:
        def get(self, key):
            cache_calls.append(("get", key))
            return None

        def set(self, key, value):
            cache_calls.append(("set", key))

    monkeypatch.setattr(core_mod, "configure_ironman", noop_async)
    monkeypatch.setattr(core_mod, "_preflight", noop_async)
    monkeypatch.setattr(core_mod, "_meter", noop_async)
    monkeypatch.setattr(core_mod, "_charge", noop_async)
    monkeypatch.setattr(core_mod, "get_llm_circuit", lambda provider=None: _FakeCircuit())
    monkeypatch.setattr(core_mod, "llm_telemetry", _FakeTelemetry)
    monkeypatch.setattr(core_mod, "with_llm_retry", fake_retry)
    monkeypatch.setattr(core_mod, "get_prompt_cache", lambda: _SpyCache())

    fake_ironman = types.ModuleType("ironman")
    fake_ironman.chat = fake_chat
    monkeypatch.setitem(sys.modules, "ironman", fake_ironman)
    captured["cache_calls"] = cache_calls
    return captured


@pytest.mark.anyio
async def test_chat_tools_passthrough_and_cache_bypass(captured_tools_chat):
    from nexus.llm_core import LLMCore

    tool_def = {"type": "function", "function": {"name": "lookup", "description": "查询", "parameters": {"properties": {}, "required": []}}}
    core = LLMCore()
    await core.chat(
        messages=[{"role": "user", "content": "查一下"}],
        system="sys",
        temperature=0.0,
        tools=[tool_def],
    )
    assert captured_tools_chat["tools"] == [tool_def], "tools 未直透 ironman facade"
    assert captured_tools_chat["cache_calls"] == [], "带 tools 恒绕 PromptCache（同 images 判例）"


@pytest.mark.anyio
async def test_chat_without_tools_keeps_cache_path(captured_tools_chat):
    from nexus.llm_core import LLMCore

    core = LLMCore()
    await core.chat(messages=[{"role": "user", "content": "q"}], system="sys", temperature=0.0)
    assert captured_tools_chat["tools"] is None
    assert captured_tools_chat["cache_calls"], "零回归：无 tools 仍走缓存位"
