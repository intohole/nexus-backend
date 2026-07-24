from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

for _suffix in ("BASE_URL", "APP_KEY", "APP_SECRET", "JWT_SECRET"):
    _old_key, _new_key = f"UC_{_suffix}", f"UC__{_suffix}"
    if _old_key in os.environ and _new_key not in os.environ:
        os.environ[_new_key] = os.environ[_old_key]

for _suffix in ("BASE_URL", "NAMESPACE"):
    _old_key, _new_key = f"LION_{_suffix}", f"LION__{_suffix}"
    if _old_key in os.environ and _new_key not in os.environ:
        os.environ[_new_key] = os.environ[_old_key]


class DatabaseConfig(BaseSettings):
    url: str = Field(default="sqlite:///./app.db", description="数据库连接URL")
    pool_size: int = Field(default=5, description="连接池大小")
    max_overflow: int = Field(default=10, description="连接池最大溢出")
    pool_recycle: int = Field(default=3600, description="连接回收时间(秒)")
    echo: bool = Field(default=False, description="是否打印SQL")
    sqlite_pragma: bool = Field(default=True, description="是否启用SQLite PRAGMA优化")

    model_config = SettingsConfigDict(extra="ignore")


class CORSConfig(BaseSettings):
    allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost"])
    allow_credentials: bool = Field(default=True)
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])

    model_config = SettingsConfigDict(extra="ignore")


class UCConfig(BaseSettings):
    base_url: str = Field(default="http://localhost:8901")
    app_key: str = Field(default="")
    app_secret: str = Field(default="")
    jwt_secret: str = Field(default="")

    model_config = SettingsConfigDict(extra="ignore")


class LionConfig(BaseSettings):
    base_url: str = Field(default="http://localhost:9527")
    namespace: str = Field(default="default")

    model_config = SettingsConfigDict(extra="ignore")


class LoggingConfig(BaseSettings):
    level: str = Field(default="INFO")
    dir: str = Field(default="logs")
    retention_days: int = Field(default=30)
    json_format: bool = Field(default=False)
    console: bool = Field(default=True)

    model_config = SettingsConfigDict(extra="ignore")


class RateLimitConfig(BaseSettings):
    enabled: bool = Field(default=True)
    requests_per_minute: int = Field(default=120)
    requests_per_hour: int = Field(default=2000)
    exclude_paths: list[str] = Field(default_factory=lambda: ["/health", "/static"])

    model_config = SettingsConfigDict(extra="ignore")


class StaticFilesConfig(BaseSettings):
    directory: str = Field(default="static")
    no_cache: bool = Field(default=True)
    spa_fallback: bool = Field(default=False)

    model_config = SettingsConfigDict(extra="ignore")


class NexusConfig(BaseSettings):
    app_name: str = Field(default="app")
    app_version: str = Field(default="1.0.0")
    debug: bool = Field(default=False)
    timezone: str = Field(default="Asia/Shanghai")
    path_prefix: str = Field(default="")

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    uc: UCConfig = Field(default_factory=UCConfig)
    lion: LionConfig = Field(default_factory=LionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    static_files: StaticFilesConfig = Field(default_factory=StaticFilesConfig)

    extra: dict[str, object] = Field(default_factory=dict)

    model_config = SettingsConfigDict(extra="allow", env_nested_delimiter="__")


def _resolve_env(value: object) -> object:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        return os.environ.get(env_key, "")
    return value


def _resolve_dict(data: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = _resolve_dict(value)
        elif isinstance(value, list):
            result[key] = [_resolve_env(v) if isinstance(v, str) else v for v in value]
        else:
            result[key] = _resolve_env(value)
    return result


class ConfigFactory:
    _instance: Optional[NexusConfig] = None
    _raw_yaml: dict[str, object] = {}
    _lock: threading.Lock = threading.Lock()

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


def configure(
    config: Optional[NexusConfig] = None,
    config_path: Optional[str | Path] = None,
    **kwargs: object,
) -> NexusConfig:
    if config is not None:
        ConfigFactory.set(config)
        return config
    if config_path is not None:
        return ConfigFactory.load_from_yaml(config_path)
    if kwargs:
        config = NexusConfig(**kwargs)
        ConfigFactory.set(config)
        return config
    return ConfigFactory.get()


def load_project_config(config_path: str | Path) -> NexusConfig:
    path: Path = Path(config_path)
    if path.exists():
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
