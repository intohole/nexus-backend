"""项目级配置装载：多文件合并、raw 树读取与 yaml_* 弱类型取值族。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from nexus.config.env import _substitute_env, _walk_env
from nexus.config.factory import ConfigFactory
from nexus.config.models import NexusConfig


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_env_tree_mode(data: object, missing: str) -> object:
    return _walk_env(data, lambda v: _substitute_env(v, anchored=False, missing=missing))


def load_project_config(
    config_path: str | Path | list[str | Path] | None = None,
    *,
    merge: bool = False,
    raw: bool = False,
    resolve_env: bool = True,
    missing: str = "keep",
) -> NexusConfig | dict[str, object]:
    if missing not in ("keep", "empty"):
        raise ValueError("missing must be 'keep' or 'empty'")
    if raw:
        if isinstance(config_path, (list, tuple)) and not merge:
            raise ValueError("load_project_config: multi-file merge requires merge=True")
        paths: list[str | Path] = config_path if isinstance(config_path, (list, tuple)) else [config_path]
        merged: dict[str, object] = {}
        for p in paths:
            if not p:
                continue
            path: Path = Path(p)
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                data: object = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                continue
            if resolve_env:
                data = _resolve_env_tree_mode(data, missing)
            merged = deep_merge(merged, data)
        return merged
    if isinstance(config_path, (list, tuple)):
        raise ValueError("load_project_config: multi-file merge requires raw=True")
    path: Path = Path(config_path) if config_path else None
    if path is not None and path.exists():
        return ConfigFactory.load_from_yaml(path)
    return ConfigFactory.get()


def yaml_get(group: str, key: str, default: str = "") -> str:
    raw: dict[str, object] = ConfigFactory.get_raw_yaml()
    group_data: object = raw.get(group)
    if isinstance(group_data, dict):
        val: object = group_data.get(key)
        if val is not None:
            return str(val)
    return os.getenv(f"{group}_{key}".upper(), default)


def yaml_secret(group: str, key: str, default: str = "") -> str:
    env_val: str | None = os.getenv(f"{group}_{key}".upper())
    if env_val:
        return env_val
    raw: dict[str, object] = ConfigFactory.get_raw_yaml()
    group_data: object = raw.get(group)
    if isinstance(group_data, dict):
        val: object = group_data.get(key)
        if val is not None:
            return str(val)
    return default


def yaml_int(group: str, key: str, default: int = 0) -> int:
    raw: dict[str, object] = ConfigFactory.get_raw_yaml()
    group_data: object = raw.get(group)
    if isinstance(group_data, dict):
        val: object = group_data.get(key)
        if val is not None:
            return int(val)
    return int(os.getenv(f"{group}_{key}".upper(), str(default)))


def yaml_float(group: str, key: str, default: float = 0.0) -> float:
    raw: dict[str, object] = ConfigFactory.get_raw_yaml()
    group_data: object = raw.get(group)
    if isinstance(group_data, dict):
        val: object = group_data.get(key)
        if val is not None:
            return float(val)
    return float(os.getenv(f"{group}_{key}".upper(), str(default)))


def yaml_bool(group: str, key: str, default: bool = False) -> bool:
    raw: dict[str, object] = ConfigFactory.get_raw_yaml()
    group_data: object = raw.get(group)
    if isinstance(group_data, dict):
        val: object = group_data.get(key)
        if val is not None:
            return str(val).lower() in ("true", "1", "yes")
    return os.getenv(f"{group}_{key}".upper(), str(default)).lower() in ("true", "1", "yes")
