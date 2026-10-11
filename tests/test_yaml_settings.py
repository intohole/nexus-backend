"""YamlSectionSettings 装载基类：section 合并/优先级/env 通道语义。"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nexus.config import YamlSectionSettings


class _AppConfig(BaseSettings):
    name: str = "demo"
    debug: bool = False
    model_config = SettingsConfigDict(extra="allow")


class _LlmConfig(BaseSettings):
    model: str = "default-model"
    temperature: float = 0.5
    model_config = SettingsConfigDict(env_prefix="LLM_")


class _MissingConfig(BaseSettings):
    threshold: int = 7


def _build_settings(yaml_path: Path, resolve_env: bool = True) -> type[YamlSectionSettings]:
    class Settings(YamlSectionSettings):
        app: _AppConfig = Field(default_factory=_AppConfig)
        llm: _LlmConfig = Field(default_factory=_LlmConfig)
        missing: _MissingConfig = Field(default_factory=_MissingConfig)
        yaml_sections: ClassVar[dict[str, type[BaseSettings]]] = {
            "app": _AppConfig,
            "llm": _LlmConfig,
            "missing": _MissingConfig,
        }
        yaml_resolve_env: ClassVar[bool] = resolve_env

        def yaml_sources(self) -> list[Path]:
            return [yaml_path]

    return Settings


def test_yaml_absent_falls_back_to_defaults(tmp_path: Path) -> None:
    Settings = _build_settings(tmp_path / "nope.yaml")
    settings = Settings()
    assert settings.app.name == "demo"
    assert settings.llm.temperature == 0.5
    assert settings.missing.threshold == 7


def test_yaml_sections_merged(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: 照妖镜\nllm:\n  model: glm-x\n",
        encoding="utf-8",
    )
    Settings = _build_settings(tmp_path / "config.yaml")
    settings = Settings()
    assert settings.app.name == "照妖镜"
    assert settings.llm.model == "glm-x"
    assert settings.llm.temperature == 0.5
    assert settings.missing.threshold == 7


def test_init_kwargs_override_yaml(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("app:\n  name: from-yaml\n", encoding="utf-8")
    Settings = _build_settings(tmp_path / "config.yaml")
    settings = Settings(app={"name": "from-kwarg"})
    assert settings.app.name == "from-kwarg"


def test_undeclared_section_ignored(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "lion:\n  base_url: http://x\napp:\n  name: a\n",
        encoding="utf-8",
    )
    Settings = _build_settings(tmp_path / "config.yaml")
    settings = Settings()
    assert settings.app.name == "a"
    assert not hasattr(settings, "lion")


def test_yaml_value_wins_over_env_but_env_fills_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text(
        "llm:\n  model: from-yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL", "from-env")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
    Settings = _build_settings(tmp_path / "config.yaml")
    settings = Settings()
    assert settings.llm.model == "from-yaml"
    assert settings.llm.temperature == 0.9


def test_resolve_env_on_by_default_and_off_keeps_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        'app:\n  name: "${SECRET_KEY:-fallback}"\n',
        encoding="utf-8",
    )
    resolved = _build_settings(tmp_path / "config.yaml", resolve_env=True)()
    assert resolved.app.name == "fallback"

    (tmp_path / "config.yaml").write_text(
        'app:\n  name: "${SECRET_KEY:-fallback}"\n',
        encoding="utf-8",
    )
    literal = _build_settings(tmp_path / "config.yaml", resolve_env=False)()
    assert literal.app.name == "${SECRET_KEY:-fallback}"


def test_prepare_yaml_hook_invoked_before_section_build(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "app:\n  name: raw\n",
        encoding="utf-8",
    )
    seen: list[str] = []

    class Settings(_build_settings(tmp_path / "config.yaml")):
        def prepare_yaml(self, raw: dict[str, object]) -> dict[str, object]:
            app = raw.get("app")
            assert isinstance(app, dict)
            app["name"] = "anchored"
            seen.append("prepared")
            return raw

    settings = Settings()
    assert seen == ["prepared"]
    assert settings.app.name == "anchored"


def test_partial_yaml_section_stays_default(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "llm:\n  temperature: 0.1\n",
        encoding="utf-8",
    )
    Settings = _build_settings(tmp_path / "config.yaml")
    settings = Settings()
    assert settings.llm.temperature == 0.1
    assert settings.llm.model == "default-model"
