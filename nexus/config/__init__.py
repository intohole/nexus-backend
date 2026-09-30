"""统一配置包：模型（models）/环境变量解析（env）/工厂（factory）/装载（loader）。

导入本包即触发 UC_/LION_ 旧版扁平环境键迁移（env 模块级副作用），
须先于任何 NexusConfig 实例化，故 env 列于首位。
"""
__import__("nexus.config.env")  # 旧键迁移副作用须先执行（pyflakes 不解析 noqa，故用此形态）
from nexus.config.env import resolve_env_string, resolve_env_tree
from nexus.config.factory import ConfigFactory, get_settings
from nexus.config.loader import (
    load_project_config,
    yaml_bool,
    yaml_float,
    yaml_get,
    yaml_int,
    yaml_secret,
)
from nexus.config.models import (
    AuditConfig,
    CORSConfig,
    DatabaseConfig,
    LionConfig,
    LoggingConfig,
    NexusConfig,
    RateLimitConfig,
    StaticFilesConfig,
    UCConfig,
)

__all__ = [
    "DatabaseConfig", "CORSConfig", "UCConfig", "LionConfig", "LoggingConfig",
    "RateLimitConfig", "AuditConfig", "StaticFilesConfig", "NexusConfig",
    "resolve_env_string", "resolve_env_tree", "ConfigFactory", "get_settings",
    "load_project_config",
    "yaml_get", "yaml_secret", "yaml_int", "yaml_float", "yaml_bool",
]
