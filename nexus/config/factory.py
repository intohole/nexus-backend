"""配置工厂：NexusConfig 单例持有与 YAML 装载。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import yaml

from nexus.config.env import _resolve_dict
from nexus.config.models import NexusConfig


class ConfigFactory:
    _instance: Optional[NexusConfig] = None
    _raw_yaml: dict[str, object] = {}
    _lock: threading.RLock = threading.RLock()

    @classmethod
    def get(cls) -> NexusConfig:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls._load_default()
        return cls._instance

    @classmethod
    def set(cls, config: NexusConfig) -> None:
        with cls._lock:
            cls._instance = config

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None
            cls._raw_yaml = {}

    @classmethod
    def get_raw_yaml(cls) -> dict[str, object]:
        return cls._raw_yaml

    @classmethod
    def load_from_yaml(cls, path: str | Path) -> NexusConfig:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        resolved = _resolve_dict(raw)
        if isinstance(resolved, dict):
            db_cfg = resolved.get("database")
            if isinstance(db_cfg, dict):
                url = db_cfg.get("url")
                if isinstance(url, str) and url.startswith("sqlite"):
                    scheme, sep, db_path = url.partition(":///")
                    if sep and db_path and not db_path.startswith("/"):
                        db_cfg["url"] = f"{scheme}:///{(path.parent / db_path).resolve()}"
        with cls._lock:
            cls._raw_yaml = resolved if isinstance(resolved, dict) else {}
            config = NexusConfig(**resolved)
            cls._instance = config
        return config

    @classmethod
    def _load_default(cls) -> NexusConfig:
        env_path = os.environ.get("NEXUS_CONFIG")
        if env_path and Path(env_path).exists():
            return cls.load_from_yaml(env_path)
        return NexusConfig()


def get_settings() -> NexusConfig:
    return ConfigFactory.get()
