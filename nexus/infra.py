from __future__ import annotations

import os

from nexus.lion import get_infra_config, get_business_config


def _compose_url(host: str, port: str, path: str = "") -> str:
    host = (host or "").strip()
    port = (port or "").strip()
    if not host:
        return ""
    url = f"http://{host}" + (f":{port}" if port else "")
    path = (path or "").strip()
    if path and not path.startswith("/"):
        path = "/" + path
    return url + path if path else url


async def _infra_url(key: str, env_key: str, default: str) -> str:
    config = await _get_infra(key)
    path = str(config.get("path") or "")
    if config.get("base_url"):
        url = str(config["base_url"])
    else:
        url = _compose_url(str(config.get("host") or ""), str(config.get("port") or ""), path)
    return url or os.getenv(env_key, default)


async def _get_infra(key: str) -> dict[str, object]:
    return await get_infra_config(key)


async def get_uc_base_url() -> str:
    return await _infra_url("usercenter", "UC_BASE_URL", "")


async def get_uc_config() -> dict[str, str]:
    config = await _get_infra("usercenter")
    base_url = await _infra_url("usercenter", "UC_BASE_URL", "")
    app_key = str(config.get("app_key") or "") or os.getenv("UC_APP_KEY", "")
    app_secret = str(config.get("app_secret") or "") or os.getenv("UC_APP_SECRET", "")
    return {"base_url": base_url, "app_key": app_key, "app_secret": app_secret}


async def get_spider_base_url() -> str:
    return await _infra_url("spider", "SPIDER_BASE_URL", "")


async def get_spider_config() -> dict[str, str]:
    config = await _get_infra("spider")
    base_url = await _infra_url("spider", "SPIDER_BASE_URL", "")
    service_token = str(config.get("service_token") or "") or os.getenv("SERVICE_TOKEN", "")
    return {"base_url": base_url, "service_token": service_token}


async def get_promptmanager_config() -> dict[str, str]:
    config = await _get_infra("promptmanager")
    base_url = await _infra_url("promptmanager", "PM_BASE_URL", "")
    api_key = str(config.get("api_key") or "") or os.getenv("PM_GATEWAY_API_KEY", "")
    gateway_url = str(config.get("gateway_url") or "") or os.getenv("PM_GATEWAY_URL", "")
    return {"base_url": base_url, "api_key": api_key, "gateway_url": gateway_url}


async def get_beememory_base_url() -> str:
    return await _infra_url("beememory", "BEEMEMORY_BASE_URL", "")


async def get_chroma_config() -> dict[str, str]:
    config = await _get_infra("chroma")
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
