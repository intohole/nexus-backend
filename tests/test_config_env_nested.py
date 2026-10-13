import tempfile
from pathlib import Path

from nexus.config import load_project_config
from nexus.config.env import _substitute_env
from nexus.config.factory import ConfigFactory


def test_anchored_nested_primary(monkeypatch):
    monkeypatch.setenv("JN_TEST_A", "http://a:1")
    monkeypatch.setenv("JN_TEST_B", "http://b:2")
    assert _substitute_env("${JN_TEST_A:-${JN_TEST_B}}", anchored=True, missing="empty") == "http://a:1"


def test_anchored_nested_fallback(monkeypatch):
    monkeypatch.delenv("JN_TEST_A", raising=False)
    monkeypatch.setenv("JN_TEST_B", "http://b:2")
    assert _substitute_env("${JN_TEST_A:-${JN_TEST_B}}", anchored=True, missing="empty") == "http://b:2"


def test_anchored_nested_both_missing(monkeypatch):
    monkeypatch.delenv("JN_TEST_A", raising=False)
    monkeypatch.delenv("JN_TEST_B", raising=False)
    assert _substitute_env("${JN_TEST_A:-${JN_TEST_B}}", anchored=True, missing="empty") == ""
    assert _substitute_env("${JN_TEST_A:-${JN_TEST_B}}", anchored=True, missing="keep") == "${JN_TEST_A:-${JN_TEST_B}}"


def test_unanchored_nested_inline(monkeypatch):
    monkeypatch.delenv("JN_TEST_A", raising=False)
    monkeypatch.setenv("JN_TEST_B", "10.0.0.1:9527")
    assert _substitute_env("http://${JN_TEST_A:-${JN_TEST_B}}/api", anchored=False, missing="keep") == "http://10.0.0.1:9527/api"


def test_single_layer_regression(monkeypatch):
    monkeypatch.setenv("JN_TEST_A", "v1")
    monkeypatch.delenv("JN_TEST_B", raising=False)
    assert _substitute_env("${JN_TEST_A:-p}", anchored=True, missing="empty") == "v1"
    assert _substitute_env("${JN_TEST_B:-p}", anchored=True, missing="empty") == "p"
    assert _substitute_env("${JN_TEST_B}", anchored=True, missing="empty") == ""
    assert _substitute_env("${JN_TEST_B:-}", anchored=True, missing="empty") == ""
    assert _substitute_env("x${JN_TEST_B}y", anchored=False, missing="keep") == "x${JN_TEST_B}y"
    assert _substitute_env("x${JN_TEST_A}y", anchored=False, missing="keep") == "xv1y"


def test_env_value_treated_literal(monkeypatch):
    monkeypatch.setenv("JN_TEST_A", "val${JN_TEST_B}")
    monkeypatch.delenv("JN_TEST_B", raising=False)
    assert _substitute_env("${JN_TEST_A:-${JN_TEST_B}}", anchored=True, missing="empty") == "val${JN_TEST_B}"


def test_load_project_config_nested(monkeypatch):
    monkeypatch.delenv("JN_TEST_LION", raising=False)
    monkeypatch.setenv("JN_TEST_NEXUS", "http://10.0.0.1:9527")
    ConfigFactory.reset()
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.yaml"
        cfg_path.write_text('lion:\n  base_url: "${JN_TEST_LION:-${JN_TEST_NEXUS}}"\n', encoding="utf-8")
        cfg = load_project_config(cfg_path)
        assert cfg.lion.base_url == "http://10.0.0.1:9527"
