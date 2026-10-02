"""通用工具门面：实现按域拆分于同目录子模块，此处聚合保持 `nexus.utils.X` 引用稳定。"""
from nexus.utils.cache import MemoryCache, SyncTTLCache  # noqa: F401
from nexus.utils.convert import clamp, paginate_from_skip, safe_bool, safe_float  # noqa: F401
from nexus.utils.http import HttpClient  # noqa: F401
from nexus.utils.math import batch_cosine_similarity, cosine_similarity  # noqa: F401
from nexus.utils.net import HealthRegistry, get_client_ip, resolve_cors_origins  # noqa: F401
from nexus.utils.retry import RetryExhausted, backoff_delay, is_retryable_error, with_retry  # noqa: F401
from nexus.utils.time import TimeUtils  # noqa: F401

__all__ = [
    "TimeUtils",
    "MemoryCache",
    "SyncTTLCache",
    "HttpClient",
    "with_retry",
    "is_retryable_error",
    "backoff_delay",
    "RetryExhausted",
    "HealthRegistry",
    "clamp",
    "safe_float",
    "safe_bool",
    "resolve_cors_origins",
    "get_client_ip",
    "cosine_similarity",
    "batch_cosine_similarity",
    "paginate_from_skip",
]
