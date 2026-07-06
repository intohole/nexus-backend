from __future__ import annotations

import os

from nexus.lion import get_infra_config, get_business_config


async def get_uc_base_url() -> str:
    config = await get_infra_config("usercenter")
    url = str(config.get("base_url") or "")
    return url or os.getenv("UC_BASE_URL", "http://localhost:8901")


async def get_uc_config() -> dict[str, str]:
    config = await get_infra_config("usercenter")
    base_url = str(config.get("base_url") or "") or os.getenv("UC_BASE_URL", "http://localhost:8901")
    app_key = str(config.get("app_key") or "") or os.getenv("UC_APP_KEY", "")
    app_secret = str(config.get("app_secret") or "") or os.getenv("UC_APP_SECRET", "")
    return {"base_url": base_url, "app_key": app_key, "app_secret": app_secret}


async def get_spider_base_url() -> str:
    config = await get_infra_config("spider")
    url = str(config.get("base_url") or "")
    return url or os.getenv("SPIDER_BASE_URL", "http://localhost:8250")


async def get_spider_config() -> dict[str, str]:
    config = await get_infra_config("spider")
    base_url = str(config.get("base_url") or "") or os.getenv("SPIDER_BASE_URL", "http://localhost:8250")
    service_token = str(config.get("service_token") or "") or os.getenv("SERVICE_TOKEN", "")
    return {"base_url": base_url, "service_token": service_token}


async def get_promptmanager_config() -> dict[str, str]:
    config = await get_infra_config("promptmanager")
    base_url = str(config.get("base_url") or "") or os.getenv("PROMPTFORGE_GATEWAY_URL", "http://localhost:8400")
    api_key = str(config.get("api_key") or "") or os.getenv("PROMPTFORGE_API_KEY", "")
    gateway_url = str(config.get("gateway_url") or "") or os.getenv("PROMPTFORGE_GATEWAY_URL", "http://localhost:8400/api/gateway/v1")
    return {"base_url": base_url, "api_key": api_key, "gateway_url": gateway_url}


async def get_beememory_base_url() -> str:
    config = await get_infra_config("beememory")
    url = str(config.get("base_url") or "")
    return url or os.getenv("BEEMEMORY_BASE_URL", "http://localhost:8700")


async def get_chroma_config() -> dict[str, str]:
    config = await get_infra_config("chroma")
    host = str(config.get("host") or "") or os.getenv("CHROMA_HOST", "localhost")
    port = str(config.get("port") or "") or os.getenv("CHROMA_PORT", "8999")
    api_key = str(config.get("api_key") or "") or os.getenv("CHROMA_API_KEY", "")
    return {"host": host, "port": port, "api_key": api_key}


async def get_rate_limit_config() -> dict[str, int]:
    config = await get_business_config("rate_limit")
    return {
        "default_rpm": int(config.get("default_rpm", 60)),
        "login_rpm": int(config.get("login_rpm", 20)),
        "register_rph": int(config.get("register_rph", 10)),
        "burst": int(config.get("burst", 10)),
    }


async def get_retry_config() -> dict[str, object]:
    config = await get_business_config("retry")
    return {
        "max_attempts": int(config.get("max_attempts", 3)),
        "backoff_factor": float(config.get("backoff_factor", 2.0)),
        "max_backoff": float(config.get("max_backoff", 60.0)),
    }


async def get_timeout_config() -> dict[str, int]:
    config = await get_business_config("timeout")
    return {
        "llm_call": int(config.get("llm_call", 60)),
        "http_request": int(config.get("http_request", 30)),
        "db_query": int(config.get("db_query", 10)),
    }


async def get_auth_config() -> dict[str, int]:
    config = await get_business_config("auth")
    return {
        "access_token_expire_minutes": int(config.get("access_token_expire_minutes", 1440)),
    }


async def get_llm_quota_config() -> dict[str, int]:
    config = await get_business_config("llm_quota")
    return {
        "daily_quota": int(config.get("daily_quota", 100)),
        "max_concurrent": int(config.get("max_concurrent", 3)),
        "queue_max_size": int(config.get("queue_max_size", 20)),
    }
