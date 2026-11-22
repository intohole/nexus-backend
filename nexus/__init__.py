from nexus.api_decorators import handle_api_errors
from nexus.asr import asr_transcribe
from nexus.audit_middleware import AuditMiddleware
from nexus.auth import (
    extract_bearer_token,
    get_current_user_full,
    get_current_user_id_optional,
    get_current_user_id_required,
)
from nexus.auth_anon import (
    get_current_user_or_anon,
    get_current_user_or_anon_lax,
    get_current_user_or_anon_optional,
)
from nexus.auth_routes import create_auth_router
from nexus.automation import (
    AutomationContext,
    AutomationEngine,
    AutomationResult,
    EventTrigger,
    ScheduleTrigger,
)
from nexus.bm_sdk import BeeMemorySDK, ensure_app_memory, get_bm_sdk
from nexus.boot import (
    mount_spa_static,
    register_health_endpoints,
    register_service_auth,
)
from nexus.business_probe import (
    build_probe_runner,
    probe_scheduler,
    register_probe_endpoints,
)
from nexus.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
    get_circuit_breaker,
)
from nexus.config import (
    load_project_config,
    yaml_bool,
    yaml_float,
    yaml_get,
    yaml_int,
    yaml_secret,
)
from nexus.cost_guard import CostBudget, CostBudgetExceededError, CostGuard, get_cost_guard
from nexus.database import Base, DatabaseManager, close_db, db_manager, get_db, init_db
from nexus.datacenter import (
    DOMAIN_ASSET,
    DOMAIN_CAREER,
    DOMAIN_CREATIVE,
    DOMAIN_GROWTH,
    DOMAIN_KNOWLEDGE,
    get_datacenter_client,
    report_core,
)
from nexus.doc_extract import extract_document_text
from nexus.errors import NotFoundError
from nexus.fastapi_setup import (
    create_app,
    register_internal_endpoints,
    setup_middleware,
    setup_static_files,
)
from nexus.image import get_image_service
from nexus.infra import get_uc_base_url
from nexus.ironman import ensure_ironman, init_ironman, is_ironman_available
from nexus.ironman import startup as startup_ironman
from nexus.jwt_utils import sign_jwt, verify_jwt
from nexus.lifespan import create_standard_lifespan
from nexus.lion import (
    get_business_config,
    get_chat_config,
    get_embed_config,
    get_image_config,
    get_lion,
)
from nexus.llm import get_llm_service
from nexus.llm_client import LLMJsonClient
from nexus.llm_helpers import disciplined_messages, with_tail_context
from nexus.llm_json import (
    extract_json_rows,
    is_llm_json_error,
    parse_json_column,
    parse_llm_json,
    parse_llm_json_lenient,
    parse_llm_json_or,
    strip_code_fence,
)
from nexus.llm_optimizer import compact_history, estimate_tokens, trim_context
from nexus.llm_utils import (
    LLMTimeoutError,
    ask_json,
    chat_json,
    is_meta_narration,
    with_llm_retry,
)
from nexus.logging import get_logger, setup_loguru
from nexus.memory import recall_context
from nexus.middleware import (
    ErrorHandlerMiddleware,
    LoadingSplashMiddleware,
    LoggingMiddleware,
    NotFoundCheckMiddleware,
    RequestIdMiddleware,
    ServiceAuthMiddleware,
    StaticAssetsCacheMiddleware,
    setup_cors,
    setup_exception_handlers,
)
from nexus.org import org_visibility
from nexus.permissions import (
    get_current_org_id_optional,
    require_admin,
    require_admin_id,
    require_permission,
)
from nexus.probe_extras import make_watermark_check
from nexus.rag_quality import repair_citations, run_check
from nexus.rate_limit import KeyedRateLimiter, RateLimitMiddleware, SlidingWindow
from nexus.repository import StatelessRepository
from nexus.resilient_llm import resilient_ask, resilient_extract, resilient_stream
from nexus.response import error_response, spa_index_response, success_response
from nexus.sanitize import sanitize_platform_text
from nexus.scheduler import get_scheduler
from nexus.service import BaseService
from nexus.sqlite_migrate import (
    ensure_column,
    ensure_columns,
    ensure_columns_sync,
    ensure_model_columns,
)
from nexus.streaming import openai_sse_deltas
from nexus.trace import TRACE_HEADER, ensure_request_id, outbound_headers
from nexus.uc_proxy import register_uc_proxy
from nexus.uc_sdk_helper import (
    close_uc_sdk,
    get_uc_sdk,
    init_uc_sdk,
    init_uc_sdk_from_lion,
    standard_err,
    standard_ok,
)
from nexus.utils import (
    MemoryCache,
    SyncTTLCache,
    TimeUtils,
    batch_cosine_similarity,
    clamp,
    cosine_similarity,
    drain_background_tasks,
    get_client_ip,
    jsonable,
    loads_or,
    pending_background_tasks,
    periodic_background_tasks,
    resolve_cors_origins,
    safe_bool,
    safe_float,
    spawn_background,
    spawn_periodic,
)
from nexus.vision import get_vision_service
from nexus.voice import register_voice_endpoints
from nexus.voice_realtime import VoiceRealtimeConfig, register_voice_realtime_endpoints
from nexus.volc import TTSStreamSession
from nexus.web_search import (
    SpiderSearchResponse,
    SpiderSearchResult,
    get_web_search_service,
)

try:
    from importlib.metadata import version as _version
    __version__ = _version("nexus-backend")
except Exception:  # source-tree import without installed metadata
    __version__ = "unknown"

__all__ = [
    "TTSStreamSession",
    "asr_transcribe", "register_voice_endpoints", "register_voice_realtime_endpoints", "VoiceRealtimeConfig",
    "load_project_config", "yaml_get", "yaml_secret", "yaml_int",
    "yaml_float", "yaml_bool", "NotFoundError", "DatabaseManager",
    "get_db", "db_manager", "Base", "init_db",
    "close_db", "setup_loguru", "get_logger", "ensure_column",
    "ensure_columns", "ensure_model_columns", "ensure_columns_sync", "success_response",
    "error_response", "spa_index_response", "TimeUtils", "MemoryCache",
    "SyncTTLCache", "cosine_similarity", "TRACE_HEADER", "ensure_request_id",
    "outbound_headers", "extract_document_text", "batch_cosine_similarity", "clamp",
    "loads_or", "safe_float", "safe_bool", "jsonable",
    "resolve_cors_origins", "spawn_background", "pending_background_tasks", "drain_background_tasks",
    "spawn_periodic", "periodic_background_tasks", "run_check", "repair_citations",
    "register_service_auth", "register_health_endpoints", "register_probe_endpoints", "build_probe_runner",
    "probe_scheduler", "make_watermark_check", "mount_spa_static", "setup_cors",
    "RequestIdMiddleware", "LoggingMiddleware", "NotFoundCheckMiddleware", "StaticAssetsCacheMiddleware",
    "LoadingSplashMiddleware", "ErrorHandlerMiddleware", "ServiceAuthMiddleware", "setup_exception_handlers",
    "RateLimitMiddleware", "SlidingWindow", "KeyedRateLimiter", "AuditMiddleware",
    "get_current_user_id_required", "get_current_user_id_optional", "get_current_user_full", "create_auth_router",
    "get_datacenter_client", "report_core", "DOMAIN_CAREER", "DOMAIN_KNOWLEDGE",
    "DOMAIN_CREATIVE", "DOMAIN_GROWTH", "DOMAIN_ASSET", "get_client_ip",
    "sanitize_platform_text", "init_uc_sdk", "init_uc_sdk_from_lion", "get_uc_sdk",
    "close_uc_sdk", "extract_bearer_token", "standard_ok", "standard_err",
    "StatelessRepository", "register_uc_proxy",
    "get_current_user_or_anon", "get_current_user_or_anon_lax", "get_current_user_or_anon_optional", "require_admin",
    "require_admin_id", "require_permission", "get_current_org_id_optional", "org_visibility",
    "create_standard_lifespan", "BaseService", "handle_api_errors", "get_lion",
    "get_chat_config", "get_embed_config", "get_image_config", "get_business_config",
    "get_uc_base_url", "get_bm_sdk", "BeeMemorySDK", "ensure_app_memory",
    "sign_jwt", "verify_jwt", "create_app", "setup_middleware",
    "setup_static_files", "register_internal_endpoints", "parse_llm_json", "parse_llm_json_lenient",
    "parse_llm_json_or", "parse_json_column", "with_llm_retry", "is_llm_json_error",
    "extract_json_rows", "ask_json", "chat_json", "is_meta_narration",
    "with_tail_context", "disciplined_messages", "recall_context", "strip_code_fence",
    "LLMTimeoutError", "get_llm_service", "LLMJsonClient", "get_image_service",
    "get_vision_service", "estimate_tokens", "trim_context", "compact_history",
    "init_ironman", "ensure_ironman", "startup_ironman", "is_ironman_available",
    "get_web_search_service", "SpiderSearchResult", "SpiderSearchResponse", "openai_sse_deltas",
    "CircuitBreaker", "CircuitBreakerConfig", "CircuitState", "CircuitBreakerOpenError",
    "get_circuit_breaker", "CostGuard", "CostBudget", "CostBudgetExceededError",
    "get_cost_guard", "resilient_ask", "resilient_extract", "resilient_stream",
    "get_scheduler", "AutomationContext", "AutomationResult", "AutomationEngine",
    "EventTrigger", "ScheduleTrigger",
]
