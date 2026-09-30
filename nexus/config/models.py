"""配置模型：数据库/CORS/UC/Lion/日志/限流/审计/静态资源等配置节。"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    base_url: str = Field(default="")
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


class AuditConfig(BaseSettings):
    enabled: bool = Field(default=True)
    app_code: str = Field(default="")
    exclude_paths: list[str] = Field(default_factory=lambda: ["/health", "/static", "/api/auth", "/login", "/docs", "/openapi.json", "/redoc"])

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
    audit: AuditConfig = Field(default_factory=AuditConfig)
    static_files: StaticFilesConfig = Field(default_factory=StaticFilesConfig)

    extra: dict[str, object] = Field(default_factory=dict)

    model_config = SettingsConfigDict(extra="allow", env_nested_delimiter="__")
