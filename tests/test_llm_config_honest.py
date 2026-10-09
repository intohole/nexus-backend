"""configure_ironman 诚实化：无 yaml、无凭证、无程序化配置时显式报错。"""
from __future__ import annotations

import asyncio

import pytest

from nexus import llm_config
from nexus.ironman_config import IronmanConfigError
from nexus.llm_config import configure_ironman, mark_ironman_configured

ENV_KEYS = ("IRONMAN_CONFIG", "IRONMAN_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "IRONMAN_MODEL")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    from ironman.config import ConfigFactory

    configured = llm_config._ironman_configured
    explicit = ConfigFactory._explicit
    llm_config._ironman_configured = False
    ConfigFactory._explicit = False
    yield
    llm_config._ironman_configured = configured
    ConfigFactory._explicit = explicit


def test_unconfigured_raises():
    with pytest.raises(IronmanConfigError, match="startup_ironman"):
        asyncio.run(configure_ironman())
    assert llm_config._ironman_configured is False


def test_env_credentials_pass(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "id.secret")
    asyncio.run(configure_ironman())
    assert llm_config._ironman_configured is True


def test_yaml_path_loads(tmp_path):
    cfg = tmp_path / "ironman.yaml"
    cfg.write_text("llm_default:\n  provider: zhipu\n  model: glm-4-flash\n  api_key: k.x\n", encoding="utf-8")
    asyncio.run(configure_ironman(yaml_path=str(cfg)))
    assert llm_config._ironman_configured is True


def test_missing_yaml_path_raises(tmp_path):
    with pytest.raises(IronmanConfigError):
        asyncio.run(configure_ironman(yaml_path=str(tmp_path / "absent.yaml")))


def test_mark_configured_short_circuits():
    mark_ironman_configured()
    asyncio.run(configure_ironman())
    assert llm_config._ironman_configured is True
