"""r264 刀D 守底：embed 回执维度 warn-only 守底（r70 2048/1024 事故判例线）。

prove-red 纪律：漂移注入用例先红（无守底时静默直返）后绿（warn 落日志）。
"""
from __future__ import annotations

import logging
from typing import Optional

import pytest

from nexus.llm_probes import (
    _EMBED_DIM_WARN_COUNT,
    _EMBED_DIM_WARN_SEEN,
    LLMProbesMixin,
    _check_embed_dimensions,
)


class _FakeBootstrap:
    def __init__(self, dimensions: Optional[int]) -> None:
        self.config = type("C", (), {})()
        self.config.embedding_default = type("E", (), {})()
        self.config.embedding_default.dimensions = dimensions


@pytest.fixture(autouse=True)
def _reset_dim_state():
    _EMBED_DIM_WARN_SEEN.clear()
    _EMBED_DIM_WARN_COUNT["warnings"] = 0
    yield
    _EMBED_DIM_WARN_SEEN.clear()
    _EMBED_DIM_WARN_COUNT["warnings"] = 0


def test_dim_mismatch_warns_once_per_combo(monkeypatch, caplog):
    monkeypatch.setattr("nexus.ironman.get_bootstrap", lambda: _FakeBootstrap(4))
    vectors = [[0.1] * 5, [0.2] * 5]
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        _check_embed_dimensions(vectors)
        _check_embed_dimensions(vectors)
    assert "embed dimension mismatch: expected=4 actual=5" in caplog.text
    assert _EMBED_DIM_WARN_COUNT["warnings"] == 1, "warn-once：同组合二调不再刷"


def test_dim_match_zero_warn(monkeypatch, caplog):
    monkeypatch.setattr("nexus.ironman.get_bootstrap", lambda: _FakeBootstrap(5))
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        _check_embed_dimensions([[0.1] * 5])
    assert "embed dimension mismatch" not in caplog.text
    assert _EMBED_DIM_WARN_COUNT["warnings"] == 0


def test_dim_expected_absent_skips(monkeypatch, caplog):
    monkeypatch.setattr("nexus.ironman.get_bootstrap", lambda: _FakeBootstrap(None))
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        _check_embed_dimensions([[0.1] * 7])
    assert "embed dimension mismatch" not in caplog.text


def test_bootstrap_absent_skips(monkeypatch, caplog):
    monkeypatch.setattr("nexus.ironman.get_bootstrap", lambda: None)
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        _check_embed_dimensions([[0.1] * 7])
    assert "embed dimension mismatch" not in caplog.text


def test_empty_result_skips(caplog):
    with caplog.at_level(logging.WARNING, logger="nexus.llm"):
        _check_embed_dimensions([])
    assert "embed dimension mismatch" not in caplog.text


def test_embed_pipeline_warn_not_block(monkeypatch, caplog):
    """prove-red：漂移注入走 embed 全链——warn 落日志且回执原样直返（恒不拦）。

    asyncio-only：embed 链（with_llm_retry/request_scope）带 asyncio 原语依赖，
    trio 后端不兼容为既有形态（非本刀引入），不进 trio 参数化。
    """
    import asyncio

    async def scenario():
        monkeypatch.setattr("nexus.ironman.get_bootstrap", lambda: _FakeBootstrap(4))

        async def fake_embed(*, text):
            return [[0.1] * 9 for _ in text]

        monkeypatch.setattr("ironman.embed", fake_embed)

        async def fake_noop(*args, **kwargs):
            return None

        # _preflight/_charge/_meter 为 llm_probes 模块级导入别名，桩面=模块路径
        monkeypatch.setattr("nexus.llm_probes.configure_ironman", fake_noop)
        monkeypatch.setattr("nexus.llm_probes._preflight", fake_noop)
        monkeypatch.setattr("nexus.llm_probes._charge", fake_noop)
        monkeypatch.setattr("nexus.llm_probes._meter", fake_noop)
        svc = object.__new__(LLMProbesMixin)
        with caplog.at_level(logging.WARNING, logger="nexus.llm"):
            result = await LLMProbesMixin.embed(svc, ["t"])
        assert result is not None, "warn-only 恒不拦截：回执必须直返"
        assert "embed dimension mismatch" in caplog.text

    asyncio.run(scenario())
