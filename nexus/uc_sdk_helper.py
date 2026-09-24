"""UC SDK 装配：初始化、Lion 凭证引导与凭证失效恢复。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from fastapi import HTTPException

from nexus.logging import get_logger
from nexus.response import success_response

logger = get_logger("nexus.uc_sdk")

_sdk: Optional[object] = None
_credential_recovery_task: Optional[asyncio.Task] = None

CREDENTIAL_RECOVERY_INITIAL_DELAY: float = 15.0
CREDENTIAL_RECOVERY_MAX_DELAY: float = 300.0


def init_uc_sdk(
    base_url: str = "",
    app_key: str = "",
    app_secret: str = "",
    jwt_secret: str = "",
) -> object:
    global _sdk
    from nexus.uc_sdk import UserCenterSDK

    base_url = base_url or os.getenv("UC_BASE_URL", "http://${UC_BASE_URL}")
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
    from nexus.infra import get_uc_auth, get_uc_base_url

    auth = await get_uc_auth()
    base_url = await get_uc_base_url()
    jwt_secret = os.getenv("UC_JWT_SECRET", "")
    sdk = init_uc_sdk(
        base_url=base_url,
        app_key=auth["app_key"],
        app_secret=auth["app_secret"],
        jwt_secret=jwt_secret,
    )
    if not auth["app_key"] or not auth["app_secret"]:
        logger.error(
            "UC 凭证缺失（Lion uc_auth 拉取失败）：登录/注册不可用，已启动后台自动恢复重试"
        )
        _schedule_credential_recovery(sdk)
    return sdk


def _schedule_credential_recovery(sdk: object) -> None:
    global _credential_recovery_task
    if _credential_recovery_task is not None and not _credential_recovery_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _credential_recovery_task = loop.create_task(_recover_credentials(sdk))


async def _recover_credentials(sdk: object) -> None:
    from nexus.infra import get_uc_auth

    delay = CREDENTIAL_RECOVERY_INITIAL_DELAY
    while True:
        await asyncio.sleep(delay)
        try:
            auth = await get_uc_auth()
        except Exception as exc:
            logger.warning(f"UC 凭证恢复重试异常: {exc}")
            auth = {}
        app_key = str(auth.get("app_key") or "")
        app_secret = str(auth.get("app_secret") or "")
        if app_key and app_secret:
            setattr(sdk, "app_key", app_key)
            setattr(sdk, "client_id", app_key)
            setattr(sdk, "app_secret", app_secret)
            setattr(sdk, "_app_secret", app_secret)
            logger.info(f"UC 凭证已从 Lion 恢复并注入 SDK: app_key={app_key}")
            await _bootstrap(sdk)
            return
        logger.warning(f"UC 凭证仍缺失（Lion uc_auth 未就绪），{int(delay)}s 后继续重试")
        delay = min(delay * 2, CREDENTIAL_RECOVERY_MAX_DELAY)


async def _bootstrap(sdk: object) -> None:
    try:
        ok: bool = await sdk.bootstrap()
        if ok:
            logger.info("UC SDK service token bootstrap success")
        else:
            logger.warning("UC SDK bootstrap failed, verify_token will use fallback")
    except Exception as exc:
        logger.warning(f"UC SDK bootstrap error: {exc}")
    start = getattr(sdk, "start_background_refresh", None)
    if start is not None:
        try:
            await start()
        except Exception as exc:
            logger.warning(f"UC SDK background refresh start error: {exc}")


def get_uc_sdk() -> object:
    if _sdk is None:
        raise RuntimeError("UC SDK not initialized, call init_uc_sdk() first")
    return _sdk


async def close_uc_sdk() -> None:
    global _sdk, _credential_recovery_task
    if _credential_recovery_task is not None and not _credential_recovery_task.done():
        _credential_recovery_task.cancel()
    _credential_recovery_task = None
    if _sdk:
        await _sdk.close()
        _sdk = None
        logger.info("UC SDK closed")


def standard_ok(data: object, message: str = "success") -> dict[str, object]:
    return success_response(data, message)


def standard_err(message: str, status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail=message)
