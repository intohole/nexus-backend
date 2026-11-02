"""nexus.bm_sdk.ensure_app_memory 原语守底：幂等记忆化、fail-open 分型、payload 兼容。"""
import asyncio

import pytest

from nexus.bm_sdk import client as bm_client
from nexus.bm_sdk import ensure_app_memory


class _FakeGate:
    def __init__(self) -> None:
        self.health_calls = 0
        self.ensure_calls: list = []
        self.healthy = True
        self.ensure_result: dict = {"success": True}
        self.ensure_error: Exception | None = None

    async def health_check(self) -> bool:
        self.health_calls += 1
        return self.healthy

    async def ensure_app_config(self, app_name=None, **kwargs):
        if self.ensure_error is not None:
            raise self.ensure_error
        self.ensure_calls.append({"app_name": app_name, **kwargs})
        return dict(self.ensure_result)


_GATE = _FakeGate()


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    bm_client._ensured_apps.clear()
    bm_client._bm_sdk = None
    _GATE.__init__()
    monkeypatch.setattr(bm_client, "get_bm_sdk", lambda app_name="default", timeout=10.0: _GATE)
    yield
    bm_client._ensured_apps.clear()
    bm_client._bm_sdk = None


def test_success_memoized():
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is True
    assert _GATE.health_calls == 1
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is True
    assert _GATE.health_calls == 1, "成功后二次调用不得再打健康探针"
    assert _GATE.ensure_calls == [
        {"app_name": "appA", "similarity_threshold": 0.8, "expiry_days": 90}
    ], "payload 必须与既有仓字面一致（skip_low_value_gate 缺席）"


def test_health_fail_memoized():
    _GATE.healthy = False
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is False
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is False
    assert _GATE.health_calls == 1, "失败也记忆化：每消息调用形态下不得重复打探针"
    assert _GATE.ensure_calls == [], "健康缺席时不得调用 ensure_app_config"


def test_ensure_rejected_memoized():
    _GATE.ensure_result = {"success": False, "message": "rejected"}
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is False
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is False
    assert _GATE.health_calls == 1 and len(_GATE.ensure_calls) == 1


def test_exception_fail_open():
    _GATE.ensure_error = RuntimeError("boom")
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is False


def test_skip_low_value_gate_payload():
    assert asyncio.run(
        ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=180, skip_low_value_gate=True)
    ) is True
    assert _GATE.ensure_calls[0]["skip_low_value_gate"] is True


def test_per_app_isolation():
    assert asyncio.run(ensure_app_memory("appA", similarity_threshold=0.8, expiry_days=90)) is True
    assert asyncio.run(ensure_app_memory("appB", similarity_threshold=0.75, expiry_days=180)) is True
    assert _GATE.health_calls == 2, "不同 app 互不共享记忆化"
