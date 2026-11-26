"""nexus 包根（渐进披露形态）：声明面只留 name，正文（子模块）首次触达才载入。

单源 _REEXPORTS：绑定名 -> 定义位置（"模块"，别名导出为 "模块:原名"）。__all__ 由映射推导。
"""
from __future__ import annotations

from importlib import import_module

try:
    from importlib.metadata import version as _version
    __version__ = _version("nexus-backend")
except Exception:  # source-tree import without installed metadata
    __version__ = "unknown"

_REEXPORTS: dict[str, str] = {
    "AgentHarness": "nexus.agent_harness",
    "HarnessProfile": "nexus.agent_harness",
    "BeeMemoryBackend": "nexus.bee_memory",
    "build_agent_harness": "nexus.agent_harness",
    "NexusSkillSource": "nexus.skill_source",
    "handle_api_errors": "nexus.api_decorators",
    "asr_transcribe": "nexus.asr",
    "AuditMiddleware": "nexus.audit_middleware",
    "extract_bearer_token": "nexus.auth",
    "get_current_user_full": "nexus.auth",
    "get_current_user_id_optional": "nexus.auth",
    "get_current_user_id_required": "nexus.auth",
    "get_current_user_or_anon": "nexus.auth_anon",
    "get_current_user_or_anon_lax": "nexus.auth_anon",
    "get_current_user_or_anon_optional": "nexus.auth_anon",
    "create_auth_router": "nexus.auth_routes",
    "AutomationContext": "nexus.automation",
    "AutomationEngine": "nexus.automation",
    "AutomationResult": "nexus.automation",
    "EventTrigger": "nexus.automation",
    "ScheduleTrigger": "nexus.automation",
    "BeeMemorySDK": "nexus.bm_sdk",
    "ensure_app_memory": "nexus.bm_sdk",
    "get_bm_sdk": "nexus.bm_sdk",
    "mount_spa_static": "nexus.boot",
    "register_health_endpoints": "nexus.boot",
    "register_service_auth": "nexus.boot",
    "build_probe_runner": "nexus.business_probe",
    "probe_scheduler": "nexus.business_probe",
    "register_probe_endpoints": "nexus.business_probe",
    "CircuitBreaker": "nexus.circuit_breaker",
    "CircuitBreakerConfig": "nexus.circuit_breaker",
    "CircuitBreakerOpenError": "nexus.circuit_breaker",
    "CircuitState": "nexus.circuit_breaker",
    "get_circuit_breaker": "nexus.circuit_breaker",
    "load_project_config": "nexus.config",
    "yaml_bool": "nexus.config",
    "yaml_float": "nexus.config",
    "yaml_get": "nexus.config",
    "yaml_int": "nexus.config",
    "yaml_secret": "nexus.config",
    "CostBudget": "nexus.cost_guard",
    "CostBudgetExceededError": "nexus.cost_guard",
    "CostGuard": "nexus.cost_guard",
    "get_cost_guard": "nexus.cost_guard",
    "Base": "nexus.database",
    "DatabaseManager": "nexus.database",
    "close_db": "nexus.database",
    "db_manager": "nexus.database",
    "get_db": "nexus.database",
    "init_db": "nexus.database",
    "DOMAIN_ASSET": "nexus.datacenter",
    "DOMAIN_CAREER": "nexus.datacenter",
    "DOMAIN_CREATIVE": "nexus.datacenter",
    "DOMAIN_GROWTH": "nexus.datacenter",
    "DOMAIN_KNOWLEDGE": "nexus.datacenter",
    "get_datacenter_client": "nexus.datacenter",
    "report_core": "nexus.datacenter",
    "extract_document_text": "nexus.doc_extract",
    "NotFoundError": "nexus.errors",
    "create_app": "nexus.fastapi_setup",
    "register_internal_endpoints": "nexus.fastapi_setup",
    "setup_middleware": "nexus.fastapi_setup",
    "setup_static_files": "nexus.fastapi_setup",
    "get_image_service": "nexus.image",
    "get_uc_base_url": "nexus.infra",
    "ensure_ironman": "nexus.ironman",
    "init_ironman": "nexus.ironman",
    "is_ironman_available": "nexus.ironman",
    "startup_ironman": "nexus.ironman:startup",
    "sign_jwt": "nexus.jwt_utils",
    "StatelessRepository": "nexus.repository",
    "verify_jwt": "nexus.jwt_utils",
    "create_standard_lifespan": "nexus.lifespan",
    "get_business_config": "nexus.lion",
    "get_chat_config": "nexus.lion",
    "get_embed_config": "nexus.lion",
    "get_image_config": "nexus.lion",
    "get_lion": "nexus.lion",
    "get_llm_service": "nexus.llm",
    "LLMJsonClient": "nexus.llm_client",
    "disciplined_messages": "nexus.llm_helpers",
    "with_tail_context": "nexus.llm_helpers",
    "extract_json_rows": "nexus.llm_json",
    "is_llm_json_error": "nexus.llm_json",
    "parse_json_column": "nexus.llm_json",
    "parse_llm_json": "nexus.llm_json",
    "parse_llm_json_lenient": "nexus.llm_json",
    "parse_llm_json_or": "nexus.llm_json",
    "strip_code_fence": "nexus.llm_json",
    "compact_history": "nexus.llm_optimizer",
    "estimate_tokens": "nexus.llm_optimizer",
    "trim_context": "nexus.llm_optimizer",
    "LLMTimeoutError": "nexus.llm_utils",
    "ask_json": "nexus.llm_utils",
    "chat_json": "nexus.llm_utils",
    "is_meta_narration": "nexus.llm_utils",
    "with_llm_retry": "nexus.llm_utils",
    "get_logger": "nexus.logging",
    "setup_loguru": "nexus.logging",
    "recall_context": "nexus.memory",
    "ErrorHandlerMiddleware": "nexus.middleware",
    "LoadingSplashMiddleware": "nexus.middleware",
    "LoggingMiddleware": "nexus.middleware",
    "NotFoundCheckMiddleware": "nexus.middleware",
    "RequestIdMiddleware": "nexus.middleware",
    "ServiceAuthMiddleware": "nexus.middleware",
    "StaticAssetsCacheMiddleware": "nexus.middleware",
    "setup_cors": "nexus.middleware",
    "setup_exception_handlers": "nexus.middleware",
    "org_visibility": "nexus.org",
    "get_current_org_id_optional": "nexus.permissions",
    "require_admin": "nexus.permissions",
    "require_admin_id": "nexus.permissions",
    "require_permission": "nexus.permissions",
    "make_watermark_check": "nexus.probe_extras",
    "repair_citations": "nexus.rag_quality",
    "run_check": "nexus.rag_quality",
    "KeyedRateLimiter": "nexus.rate_limit",
    "RateLimitMiddleware": "nexus.rate_limit",
    "SlidingWindow": "nexus.rate_limit",
    "resilient_ask": "nexus.resilient_llm",
    "resilient_extract": "nexus.resilient_llm",
    "resilient_stream": "nexus.resilient_llm",
    "error_response": "nexus.response",
    "spa_index_response": "nexus.response",
    "success_response": "nexus.response",
    "sanitize_platform_text": "nexus.sanitize",
    "get_scheduler": "nexus.scheduler",
    "BaseService": "nexus.service",
    "ensure_column": "nexus.sqlite_migrate",
    "ensure_columns": "nexus.sqlite_migrate",
    "ensure_columns_sync": "nexus.sqlite_migrate",
    "ensure_model_columns": "nexus.sqlite_migrate",
    "openai_sse_deltas": "nexus.streaming",
    "TRACE_HEADER": "nexus.trace",
    "ensure_request_id": "nexus.trace",
    "outbound_headers": "nexus.trace",
    "register_uc_proxy": "nexus.uc_proxy",
    "close_uc_sdk": "nexus.uc_sdk_helper",
    "get_uc_sdk": "nexus.uc_sdk_helper",
    "init_uc_sdk": "nexus.uc_sdk_helper",
    "init_uc_sdk_from_lion": "nexus.uc_sdk_helper",
    "standard_err": "nexus.uc_sdk_helper",
    "standard_ok": "nexus.uc_sdk_helper",
    "MemoryCache": "nexus.utils",
    "SyncTTLCache": "nexus.utils",
    "TimeUtils": "nexus.utils",
    "batch_cosine_similarity": "nexus.utils",
    "clamp": "nexus.utils",
    "cosine_similarity": "nexus.utils",
    "drain_background_tasks": "nexus.utils",
    "get_client_ip": "nexus.utils",
    "jsonable": "nexus.utils",
    "loads_or": "nexus.utils",
    "pending_background_tasks": "nexus.utils",
    "periodic_background_tasks": "nexus.utils",
    "resolve_cors_origins": "nexus.utils",
    "safe_bool": "nexus.utils",
    "safe_float": "nexus.utils",
    "spawn_background": "nexus.utils",
    "spawn_periodic": "nexus.utils",
    "get_vision_service": "nexus.vision",
    "register_voice_endpoints": "nexus.voice",
    "VoiceRealtimeConfig": "nexus.voice_realtime",
    "register_voice_realtime_endpoints": "nexus.voice_realtime",
    "TTSStreamSession": "nexus.volc",
    "SpiderSearchResponse": "nexus.web_search",
    "SpiderSearchResult": "nexus.web_search",
    "get_web_search_service": "nexus.web_search",
}

__all__ = list(_REEXPORTS)


def __getattr__(name: str):
    target = _REEXPORTS.get(name)
    if target is not None:
        module_name, _, attr = target.partition(":")
        value = getattr(import_module(module_name), attr or name)
        globals()[name] = value
        return value
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        value = import_module(f"nexus.{name}")
    except ModuleNotFoundError as exc:
        if exc.name == f"nexus.{name}":
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
        raise
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    import pkgutil

    subs = {m.name for m in pkgutil.iter_modules(__path__)}
    return sorted(set(globals()) | set(_REEXPORTS) | subs)
