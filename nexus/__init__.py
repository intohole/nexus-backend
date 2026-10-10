from nexus.config import (
    NexusConfig,
    deep_merge,
    get_settings,
    load_project_config,
    yaml_get,
    yaml_secret,
    yaml_int,
    yaml_float,
    yaml_bool,
    resolve_env_tree,
)
from nexus.errors import (
    NexusError,
    DatabaseError,
    NotFoundError,
    RateLimitError,
    ForbiddenError,
    ContentFilterError,
)
from nexus.context import get_user_id
from nexus.database import DatabaseManager, get_db, db_manager, Base, init_db, close_db
from nexus.sqlite_migrate import (
    ensure_column,
    ensure_columns,
    ensure_model_columns,
    ensure_column_sync,
    ensure_columns_sync,
    ensure_model_columns_sync,
)
from nexus.logging import setup_logging, setup_loguru, get_logger
from nexus.response import (
    success_response,
    error_response,
    paginate_response,
    paginated_payload,
    spa_index_response,
)
from nexus.doc_extract import DocExtractError, extract_document_text

from nexus.utils import (
    TimeUtils,
    MemoryCache,
    SyncTTLCache,
    HttpClient,
    cosine_similarity,
    batch_cosine_similarity,
    clamp,
    safe_float,
    safe_bool,
    loads_or,
    resolve_cors_origins,
    get_client_ip,
    paginate_from_skip,
)
from nexus.boot import (
    register_service_auth,
    register_health_detail,
    mount_spa_static,
)
from nexus.middleware import (
    setup_cors,
    RequestIdMiddleware,
    LoggingMiddleware,
    NotFoundCheckMiddleware,
    StaticAssetsCacheMiddleware,
    LoadingSplashMiddleware,
    ErrorHandlerMiddleware,
    ServiceAuthMiddleware,
    setup_exception_handlers,
)
from nexus.rate_limit import KeyedRateLimiter, RateLimitMiddleware, SlidingWindow, TokenBucket
from nexus.audit_middleware import AuditMiddleware
from nexus.audit import log_audit
from nexus.config import RateLimitConfig as RateLimitConfig
from nexus.auth import (
    AuthDependencies,
    get_current_user_id_required,
    get_current_user_id_optional,
    get_current_user_full,
    get_current_user_full_normalized,
    get_user_string_id,
    parse_user_id,
    extract_bearer_token,
)
from nexus.auth_anon import (
    ANON_ID_RE,
    get_current_user_or_anon,
    get_current_user_or_anon_lax,
    get_current_user_or_anon_optional,
)
from nexus.permissions import require_admin, require_admin_id
from nexus.auth_routes import create_auth_router
from nexus.uc_sdk_helper import (
    init_uc_sdk,
    init_uc_sdk_from_lion,
    get_uc_sdk,
    close_uc_sdk,
    standard_ok,
    standard_err,
)
from nexus.repository import StatelessRepository, paginate, paginate_skip
from nexus.storage import read_limited
from nexus.uc_proxy import register_uc_proxy
from nexus.lifespan import create_standard_lifespan
from nexus.service import BaseService
from nexus.api_decorators import handle_api_errors
from nexus.lion import (
    get_lion,
    get_chat_config,
    get_embed_config,
    get_image_config,
    get_infra_config,
    get_business_config,
    clear_lion_cache,
)
from nexus.infra import (
    get_uc_base_url,
    get_uc_config,
    get_spider_base_url,
    get_spider_config,
    get_promptmanager_config,
    get_beememory_base_url,
    get_chroma_config,
    get_rate_limit_config,
    get_retry_config,
    get_timeout_config,
    get_auth_config,
    get_llm_quota_config,
)
from nexus.service_client import get_service_client, get_service_token
from nexus.voice import register_voice_endpoints
from nexus.user_auth import create_user_auth, get_bearer_token
from nexus.fastapi_setup import (
    create_app,
    setup_middleware,
    setup_static_files,
    register_internal_endpoints,
    AppLifecycle,
)
from nexus.datacenter import (
    get_datacenter_client,
    report_core,
    DOMAIN_CAREER,
    DOMAIN_KNOWLEDGE,
    DOMAIN_CREATIVE,
    DOMAIN_GROWTH,
    DOMAIN_ASSET,
)
from nexus.llm_utils import (
    parse_llm_json,
    parse_llm_json_lenient,
    parse_llm_json_or,
    is_llm_json_error,
    parse_json_column,
    find_balanced_json,
    with_llm_retry,
    strip_code_fence,
    LLMTimeoutError,
)
from nexus.llm import get_llm_service
from nexus.sse_manager import (
    SSEManager,
    SSEConnectionError,
    sse_event_generator,
)
from nexus.channels import (
    NotificationChannel,
    VALID_CHANNELS,
    ChannelDispatcher,
)
from nexus.llm_client import LLMJsonClient
from nexus.credits import (
    get_credits_service,
    PrecheckResult,
    ConsumeResult,
    CreditsInsufficientError,
    charged,
    credits_user_scope,
)
from nexus.image import get_image_service
from nexus.vision import get_vision_service
from nexus.llm_optimizer import (
    JSON_ONLY_HINT,
    estimate_tokens,
    trim_context,
    compact_history,
)
from nexus.ironman import (
    init_ironman,
    ensure_ironman,
    startup as startup_ironman,
    get_bootstrap,
    is_ironman_available,
    reload_ironman,
)
from nexus.bm_sdk import BeeMemorySDK, get_bm_sdk
from nexus.moutain import MoutainClient, get_moutain_client
from nexus.jwt_utils import sign_jwt, verify_jwt
from nexus.web_search import (
    get_web_search_service,
    SpiderSearchResult,
    SpiderSearchResponse,
)
from nexus.deep_research import get_deep_research_service
from nexus.streaming import (
    sse_event_dict,
    sse_data_line,
    sse_response,
    sse_chat_stream_v2,
    openai_sse_deltas,
)
from nexus.time_budget import (
    GenerationBudget,
    set_generation_budget,
    current_budget,
)
from nexus.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitMetrics,
    CircuitBreakerOpenError,
    get_circuit_breaker,
)
from nexus.cost_guard import (
    CostGuard,
    CostBudget,
    BudgetPeriod,
    TokenUsage,
    CostBudgetExceededError,
    get_cost_guard,
    configure_pricing,
)
from nexus.llm_rate_limiter import LLMRateLimiter
from nexus.resilient_llm import resilient_ask, resilient_extract, resilient_stream
from nexus.scheduler import (
    JobManager,
    get_scheduler,
    get_thread_scheduler,
)
from nexus.crontab import (
    CronScheduler,
    next_run_at,
)
from nexus.automation import (
    AutomationContext,
    AutomationResult,
    AutomationEngine,
    EventTrigger,
    ScheduleTrigger,
)
from nexus.sanitize import sanitize_agent_output, sanitize_platform_text, sanitize_text_stream

try:
    from importlib.metadata import version as _version
    __version__ = _version("nexus-backend")
except Exception:  # source-tree import without installed metadata
    __version__ = "unknown"

__all__ = [
    "register_voice_endpoints",
    "NexusConfig", "get_settings", "load_project_config", "deep_merge", "yaml_get",
    "yaml_secret", "yaml_int", "yaml_float", "yaml_bool", "NexusError",
    "DatabaseError", "NotFoundError",
    "RateLimitError", "ForbiddenError",
    "ContentFilterError", "get_user_id", "DatabaseManager", "get_db", "db_manager", "Base",
    "init_db", "close_db", "setup_logging", "setup_loguru", "get_logger",
    "ensure_column", "ensure_columns", "ensure_model_columns",
    "ensure_column_sync", "ensure_columns_sync", "ensure_model_columns_sync",
    "success_response", "error_response", "paginate_response", "paginated_payload", "spa_index_response",
    "TimeUtils", "MemoryCache", "SyncTTLCache", "MoutainClient", "HttpClient", "cosine_similarity",
    "extract_document_text", "DocExtractError",
    "validate_upload", "read_upload_with_limit", "split_ext",
    "batch_cosine_similarity", "clamp", "loads_or", "safe_float", "safe_bool", "resolve_cors_origins",
    "paginate_from_skip", "register_service_auth", "register_health_detail",
    "mount_spa_static", "setup_cors", "RequestIdMiddleware",
    "LoggingMiddleware", "NotFoundCheckMiddleware", "StaticAssetsCacheMiddleware",
    "LoadingSplashMiddleware", "ErrorHandlerMiddleware", "ServiceAuthMiddleware",
    "setup_exception_handlers", "RateLimitMiddleware", "SlidingWindow", "TokenBucket", "KeyedRateLimiter", "RateLimitConfig",
    "AuditMiddleware", "log_audit", "AuthDependencies", "get_current_user_id_required",
    "get_current_user_id_optional", "get_current_user_full",
    "get_current_user_full_normalized", "get_user_string_id", "parse_user_id",
    "create_auth_router", "create_user_auth", "get_bearer_token", "get_service_client",
    "get_service_token", "get_datacenter_client", "report_core", "DOMAIN_CAREER",
    "DOMAIN_KNOWLEDGE", "DOMAIN_CREATIVE", "DOMAIN_GROWTH", "DOMAIN_ASSET",
    "get_client_ip", "sanitize_platform_text", "resolve_env_tree", "init_uc_sdk",
    "init_uc_sdk_from_lion", "get_uc_sdk", "close_uc_sdk", "extract_bearer_token",
    "standard_ok", "standard_err", "StatelessRepository", "read_limited", "paginate", "paginate_skip",
    "register_uc_proxy",
    "ANON_ID_RE", "get_current_user_or_anon", "get_current_user_or_anon_lax",
    "get_current_user_or_anon_optional",
    "require_admin", "require_admin_id",
    "create_standard_lifespan", "BaseService", "handle_api_errors", "get_lion",
    "get_chat_config", "get_embed_config", "get_image_config", "get_infra_config",
    "get_business_config", "clear_lion_cache", "get_uc_base_url", "get_uc_config",
    "get_spider_base_url", "get_spider_config", "get_promptmanager_config",
    "get_beememory_base_url", "get_chroma_config", "get_rate_limit_config",
    "get_retry_config", "get_timeout_config", "get_auth_config", "get_llm_quota_config",
    "get_moutain_client", "get_bm_sdk", "BeeMemorySDK", "sign_jwt", "verify_jwt", "create_app", "setup_middleware",
    "setup_static_files", "register_internal_endpoints", "AppLifecycle", "parse_llm_json",
    "parse_llm_json_lenient", "parse_llm_json_or", "parse_json_column", "find_balanced_json", "with_llm_retry",
    "is_llm_json_error",
    "strip_code_fence", "LLMTimeoutError", "get_llm_service", "LLMJsonClient",
    "get_image_service", "get_vision_service", "JSON_ONLY_HINT", "estimate_tokens",
    "get_credits_service", "charged", "credits_user_scope", "PrecheckResult",
    "ConsumeResult", "CreditsInsufficientError",
    "trim_context", "compact_history", "init_ironman", "ensure_ironman", "startup_ironman",
    "get_bootstrap", "is_ironman_available", "reload_ironman", "get_web_search_service",
    "SpiderSearchResult", "SpiderSearchResponse", "get_deep_research_service", "sse_event_dict", "sse_data_line",
    "sse_response", "sse_chat_stream_v2", "SSEManager", "openai_sse_deltas",
    "GenerationBudget", "set_generation_budget", "current_budget",
    "SSEConnectionError", "sse_event_generator", "NotificationChannel", "VALID_CHANNELS",
    "ChannelDispatcher", "CircuitBreaker", "CircuitBreakerConfig", "CircuitState",
    "CircuitMetrics", "CircuitBreakerOpenError", "get_circuit_breaker", "CostGuard",
    "CostBudget", "BudgetPeriod", "TokenUsage", "CostBudgetExceededError",
    "get_cost_guard", "configure_pricing", "LLMRateLimiter", "resilient_ask",
    "resilient_extract", "resilient_stream", "get_scheduler", "JobManager",
    "get_thread_scheduler", "CronScheduler", "next_run_at", "AutomationContext",
    "AutomationResult", "AutomationEngine", "EventTrigger", "ScheduleTrigger",
    "sanitize_agent_output", "sanitize_text_stream",
]
