"""yaml section → 嵌套 Settings 装载基类：config.yaml 各 section init 期合并进 pydantic-settings。"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Mapping

from pydantic_settings import BaseSettings, SettingsConfigDict

from nexus.config.loader import load_project_config


class YamlSectionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    yaml_sections: ClassVar[Mapping[str, type[BaseSettings]]] = {}
    yaml_resolve_env: ClassVar[bool] = True

    def yaml_sources(self) -> list[Path]:
        raise NotImplementedError("yaml_sources() 须返回 config.yaml 路径清单")

    def prepare_yaml(self, raw: dict[str, object]) -> dict[str, object]:
        return raw

    def _load_raw_yaml(self) -> dict[str, object]:
        config: dict[str, object] = load_project_config(
            self.yaml_sources(),
            merge=True,
            raw=True,
            missing="empty",
            resolve_env=self.yaml_resolve_env,
        )
        return config

    def __init__(self, **kwargs: object) -> None:
        raw = self._load_raw_yaml()
        if raw:
            raw = self.prepare_yaml(raw)
            for name, section_cls in self.yaml_sections.items():
                if name in raw:
                    raw[name] = section_cls(**raw[name])
            kwargs = {**raw, **kwargs}
        super().__init__(**kwargs)
