"""档位链全列穿透守底（r94/B1）：全档×全列省略传参端到端 + 签名静态面 + 组合面.

#207 族（r90 判例）：签名/构造器/setdefault 任一层出现具体默认值，档位列对该
入口即死链——r90 只钉温度列，其余列幸存仅因默认恰为 None（运气防线）。本守底按
「fix the class, not the instance」（LLMGateway 2026-10 安基实践）把运气换成断言：
1. 动态面：全档×全列省略传参端到端，captured LLMOptions 逐列对 TASK_BUDGETS 值；
2. 静态面：入口族签名档位形参默认必须 None（inspect.signature）；
3. 组合面：facade ask_json/chat_json 与 LLMJsonClient 构造器不得在档位系统外
   硬编码温度/预算遮蔽档位（r64 时代遗产 setdefault 0.2/1500、构造器 0.3/3000，
   r94 改档位兼容形态）；无 task_type 调用保持遗产兜底（0.2/1500）钉死防漂移。
显式传参恒优先（叠加语义）由动态面抽检钉住。
"""
from __future__ import annotations

import inspect
import time
import types

import pytest

import nexus.llm_core as core_mod
import nexus.llm_utils as llm_utils_mod
from ironman.types import LLMOptions
from nexus.llm import LLMService
from nexus.llm_budget import (
    DEFAULT_THINKING_BUDGET,
    TASK_BUDGETS,
    OutputMode,
    TaskBudget,
    resolve_effective_budget,
)
from nexus.llm_client import LLMJsonClient
from nexus.llm_core import LLMCore
from nexus.llm_helpers import CONCISENESS_HINT, JSON_ONLY_HINT, PROSE_HINT, apply_output_discipline
from nexus.llm_probes import LLMProbesMixin
from nexus.llm_stream import LLMStreamMixin

_TIER_PARAMS = {
    "temperature",
    "max_tokens",
    "output_mode",
    "enable_thinking",
    "thinking_budget",
    "task_type",
}

_MODE_HINTS = {
    OutputMode.JSON: JSON_ONLY_HINT,
    OutputMode.PROSE: PROSE_HINT,
    OutputMode.CONCISE: CONCISENESS_HINT,
}


class _FakeCircuit:
    async def call(self, fn):
        return await fn()


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


@pytest.fixture()
def captured_chat(monkeypatch):
    captured = {}

    class _FakeResp:
        content = "{}"
        model = "deepseek-flash"
        cost_usd = 0.0
        latency_ms = 1.0
        provider = "test"
        cached = False
        usage = None

    async def fake_chat(messages, llm):
        captured["llm"] = llm
        captured["messages"] = messages
        return _FakeResp()

    async def fake_chat_stream(messages, llm):
        captured["llm"] = llm
        captured["messages"] = messages

        class _FakeChunk:
            content = "chunk"
            usage = None
            model = "deepseek-flash"

        yield _FakeChunk()

    async def noop_async(*args, **kwargs):
        return None

    async def fake_retry(fn, timeout, retries):
        return await fn()

    monkeypatch.setattr(core_mod, "configure_ironman", noop_async)
    monkeypatch.setattr(core_mod, "_preflight", noop_async)
    monkeypatch.setattr(core_mod, "_meter", noop_async)
    monkeypatch.setattr(core_mod, "_charge", noop_async)
    monkeypatch.setattr(core_mod, "get_llm_circuit", lambda: _FakeCircuit())
    monkeypatch.setattr(core_mod, "llm_telemetry", _FakeTelemetry)
    monkeypatch.setattr(core_mod, "with_llm_retry", fake_retry)

    fake_ironman = types.ModuleType("ironman")
    fake_ironman.chat = fake_chat
    fake_ironman.chat_stream = fake_chat_stream
    monkeypatch.setitem(__import__("sys").modules, "ironman", fake_ironman)
    return captured


# ---------------------------------------------------------------------------
# 动态面：全档 × 全列省略传参端到端
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("task_type,budget", sorted(TASK_BUDGETS.items()), ids=sorted(TASK_BUDGETS))
async def test_chat_omitted_params_take_tier_all_columns(captured_chat, task_type, budget):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type=task_type)
    llm: LLMOptions = captured_chat["llm"]
    expected_temp = budget.temperature if budget.temperature is not None else 0.7
    expected_budget = budget.thinking_budget if budget.thinking else DEFAULT_THINKING_BUDGET
    assert llm.temperature == expected_temp, task_type
    assert llm.max_tokens == budget.max_tokens, task_type
    assert llm.enable_thinking is bool(budget.thinking), task_type
    assert llm.thinking_budget == expected_budget, task_type


@pytest.mark.anyio
@pytest.mark.parametrize("task_type,budget", sorted(TASK_BUDGETS.items()), ids=sorted(TASK_BUDGETS))
async def test_chat_output_mode_column_reaches_system(captured_chat, task_type, budget):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type=task_type)
    system_content = captured_chat["messages"][0].content
    hint = _MODE_HINTS.get(budget.output_mode)
    if hint is None:
        assert PROSE_HINT not in system_content
        assert JSON_ONLY_HINT not in system_content
    else:
        assert hint in system_content, task_type


@pytest.mark.anyio
async def test_explicit_params_win_over_tier_all_columns(captured_chat):
    svc = LLMService()
    await svc.chat(
        [{"role": "user", "content": "x"}],
        task_type="extract",
        temperature=0.9,
        max_tokens=99,
        output_mode=OutputMode.PROSE,
        enable_thinking=True,
        thinking_budget=64,
    )
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.9
    assert llm.max_tokens == 99
    assert llm.enable_thinking is True
    assert llm.thinking_budget == 64
    assert PROSE_HINT in captured_chat["messages"][0].content


@pytest.mark.anyio
async def test_stream_chat_columns_converge_same_tier(captured_chat):
    svc = LLMService()
    chunks = [c async for c in svc.stream_chat([{"role": "user", "content": "x"}], task_type="creative")]
    assert chunks
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.85
    assert llm.max_tokens == 8000
    assert llm.enable_thinking is True
    assert llm.thinking_budget == 8192


# ---------------------------------------------------------------------------
# 组合面：facade ask_json/chat_json 档位兼容
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_facade_ask_json_tiered_call_rides_tier(captured_chat):
    svc = LLMService()
    result = await svc.ask_json("p", task_type="extract")
    assert isinstance(result, dict)
    llm: LLMOptions = captured_chat["llm"]
    assert llm.max_tokens == TASK_BUDGETS["extract"].max_tokens
    assert llm.temperature == TASK_BUDGETS["extract"].temperature


@pytest.mark.anyio
async def test_facade_chat_json_tiered_call_rides_tier(captured_chat):
    svc = LLMService()
    result = await svc.chat_json([{"role": "user", "content": "x"}], task_type="assistant")
    assert isinstance(result, dict)
    llm: LLMOptions = captured_chat["llm"]
    assert llm.max_tokens == TASK_BUDGETS["assistant"].max_tokens


@pytest.mark.anyio
async def test_facade_tierless_call_keeps_legacy_fallback(captured_chat):
    """无 task_type 调用保持 r64 时代 JSON 原语兜底（0.2/1500）——行为面钉死防漂移。"""
    svc = LLMService()
    await svc.ask_json("p")
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.2
    assert llm.max_tokens == 1500


@pytest.mark.anyio
async def test_facade_explicit_still_wins(captured_chat):
    svc = LLMService()
    await svc.ask_json("p", task_type="extract", max_tokens=1500, temperature=0.5)
    llm: LLMOptions = captured_chat["llm"]
    assert llm.max_tokens == 1500
    assert llm.temperature == 0.5


# ---------------------------------------------------------------------------
# 组合面：LLMJsonClient 构造器档位兼容
# ---------------------------------------------------------------------------

class _CaptureSvc:
    def __init__(self, responses):
        self._responses = list(responses)
        self.ask_kwargs = None

    async def ask(self, **kwargs):
        self.ask_kwargs = kwargs
        return self._responses.pop(0)

    async def chat(self, **kwargs):
        self.ask_kwargs = kwargs
        return self._responses.pop(0)


@pytest.fixture()
def stubbed_service(monkeypatch):
    holder: dict = {}

    def _install(responses):
        svc = _CaptureSvc(responses)
        holder["svc"] = svc
        import nexus.llm_client as llm_client_mod

        monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: svc)
        return svc

    return _install


@pytest.mark.anyio
async def test_client_tiered_construction_rides_tier(captured_chat):
    """构造器未表态（None）→ 档位值端到端到网关入口（真服务链）。"""
    client = LLMJsonClient(task_type="extract")
    result = await client.ask_json("p", "s")
    assert isinstance(result, dict)
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == TASK_BUDGETS["extract"].temperature
    assert llm.max_tokens == TASK_BUDGETS["extract"].max_tokens


@pytest.mark.anyio
async def test_client_explicit_params_win(stubbed_service):
    """goldenStock/portfolio_vision 形态（显式 0.1/2000）恒不回档。"""
    svc = stubbed_service(["{}"])
    client = LLMJsonClient(task_type="vision", temperature=0.1, max_tokens=2000)
    await client.ask_json("p", "s")
    kwargs = svc.ask_kwargs
    assert kwargs["temperature"] == 0.1
    assert kwargs["max_tokens"] == 2000


@pytest.mark.anyio
async def test_client_budget_retry_unknown_budget_safe(stubbed_service):
    """预算未知（max_tokens=None）时解析失败放大重试不 TypeError，按上限 16000 重试。"""
    svc = stubbed_service(["不是JSON", '{"answer": 1}'])
    client = LLMJsonClient(max_tokens=None)
    result = await client.ask_json("p", "s")
    assert result == {"answer": 1}
    assert svc.ask_kwargs["max_tokens"] == 16000


@pytest.mark.anyio
async def test_client_chat_path_flows_none(stubbed_service):
    svc = stubbed_service(["ok"])
    client = LLMJsonClient()
    out = await client.chat([{"role": "user", "content": "x"}], "sys")
    assert out == "ok"
    kwargs = svc.ask_kwargs
    assert kwargs["temperature"] is None
    assert kwargs["task_type"] is None


# ---------------------------------------------------------------------------
# 静态面：签名档位形参默认必须 None + facade 源面无 setdefault 遮蔽
# ---------------------------------------------------------------------------

def _tier_param_defaults(fn) -> dict[str, object]:
    sig = inspect.signature(fn)
    return {
        name: param.default
        for name, param in sig.parameters.items()
        if name in _TIER_PARAMS and param.kind not in (param.VAR_KEYWORD, param.VAR_POSITIONAL)
    }


def test_entry_signatures_tier_params_default_none():
    entries = [
        LLMCore.chat,
        LLMCore.ask,
        LLMCore.chat_with_usage,
        LLMStreamMixin.stream_chat,
        LLMStreamMixin.stream_ask,
        LLMProbesMixin.extract,
        llm_utils_mod.ask_json,
        llm_utils_mod.chat_json,
        apply_output_discipline,
        LLMJsonClient.__init__,
        LLMJsonClient.ask_json,
        LLMJsonClient.chat,
        LLMJsonClient.ask_json_multimodal,
    ]
    violations: list[str] = []
    for fn in entries:
        for name, default in _tier_param_defaults(fn).items():
            if default is not None:
                violations.append(f"{fn.__qualname__}.{name}={default!r}")
    assert violations == [], f"档位形参具体默认值遮蔽档位（#207 族）：{violations}"


def test_facade_source_has_no_setdefault_on_tier_params():
    """facade 方法源面零 setdefault：参数兜底单源在 _apply_json_primitive_fallback
    （守卫分支内联形态），facade 内联 setdefault 回潮即红。"""
    for fn in (LLMService.ask_json, LLMService.chat_json):
        src = inspect.getsource(fn)
        assert "setdefault(" not in src, f"{fn.__qualname__} 内联 setdefault 回潮（应走 _apply_json_primitive_fallback 单源）"


def test_json_primitive_fallback_single_source():
    from nexus import llm as llm_mod

    src = inspect.getsource(llm_mod._apply_json_primitive_fallback)
    assert 'setdefault("temperature", 0.2)' in src
    assert 'setdefault("max_tokens", 1500)' in src
    assert 'kwargs.get("task_type") is None' in src


# ---------------------------------------------------------------------------
# 档位表本体：0.0 温度档不被 or-trap 吞成 0.7
# ---------------------------------------------------------------------------

def test_tier_zero_temperature_not_swallowed_by_or_fallback(monkeypatch):
    monkeypatch.setitem(TASK_BUDGETS, "_zero_tier", TaskBudget(max_tokens=500, temperature=0.0))
    _mt, temp, *_rest = resolve_effective_budget("_zero_tier", None, None, None)
    assert temp == 0.0


def test_tier_none_temperature_resolves_core_default():
    _mt, temp, *_rest = resolve_effective_budget("assistant", None, None, None)
    assert temp == 0.7
