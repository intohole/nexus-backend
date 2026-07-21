from __future__ import annotations

import asyncio
import os
from typing import Optional

from fastapi import HTTPException

from nexus.logging import get_logger
from nexus.response import success_response

logger = get_logger("nexus.uc_sdk")

_sdk: Optional[object] = None


def init_uc_sdk(
    base_url: str = "",
    app_key: str = "",
    app_secret: str = "",
    jwt_secret: str = "",
) -> object:
    global _sdk
    from uc_sdk import UserCenterSDK

    base_url = base_url or os.getenv("UC_BASE_URL", "http://localhost:8901")
    app_key = app_key or os.getenv("UC_APP_KEY", "")
    app_secret = app_secret or os.getenv("UC_APP_SECRET", "")
    jwt_secret = jwt_secret or os.getenv("UC_JWT_SECRET", "")

    _sdk = UserCenterSDK(
        base_url=base_url,
        app_key=app_key,
        app_secret=app_secret,
        jwt_secret_key=jwt_secret,
    )
    logger.info(f"UC SDK initialized: base_url={base_url}, app_key={app_key}")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_bootstrap(_sdk))
    except RuntimeError:
        pass

    # 自动注入到 AuthDependencies，替代各应用 _ensure_nexus_configured()
    try:
        from nexus.auth import configure_uc_sdk
        configure_uc_sdk(_sdk)
    except Exception as exc:
        logger.debug(f"Auto-inject into AuthDependencies skipped: {exc}")

    return _sdk


async def init_uc_sdk_from_lion() -> object:
    from nexus.infra import get_uc_config

    cfg = await get_uc_config()
    jwt_secret = os.getenv("UC_JWT_SECRET", "")
    return init_uc_sdk(
        base_url=cfg["base_url"],
        app_key=cfg["app_key"],
        app_secret=cfg["app_secret"],
        jwt_secret=jwt_secret,
    )


async def _bootstrap(sdk: object) -> None:
    try:
        ok: bool = await sdk.bootstrap()
        if ok:
            logger.info("UC SDK service token bootstrap success")
        else:
            logger.warning("UC SDK bootstrap failed, verify_token will use fallback")
    except Exception as exc:
        logger.warning(f"UC SDK bootstrap error: {exc}")


def get_uc_sdk() -> object:
    if _sdk is None:
        raise RuntimeError("UC SDK not initialized, call init_uc_sdk() first")
    return _sdk


async def close_uc_sdk() -> None:
    global _sdk
    if _sdk:
        await _sdk.close()
        _sdk = None
        logger.info("UC SDK closed")


def standard_ok(data: object, message: str = "success") -> dict[str, object]:
    return success_response(data, message)


def standard_err(message: str, status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail=message)
