import logging
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from nexus.uc_sdk.client import UserCenterSDK

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def create_auth_dependencies(uc_sdk: UserCenterSDK, auto_bootstrap: bool = False):
    if auto_bootstrap and uc_sdk._app_secret:
        import asyncio

        async def _do_bootstrap():
            ok = await uc_sdk.bootstrap()
            if ok:
                logger.info("SDK service token自动引导成功")
            else:
                logger.warning("SDK service token自动引导失败，将使用普通模式")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_bootstrap())
        except RuntimeError:
            asyncio.run(_do_bootstrap())

    async def _validate_token(token: str) -> dict | None:
        if not uc_sdk.is_configured():
            raise HTTPException(
                status_code=503,
                detail="用户中心未配置，请联系管理员设置 UC_BASE_URL 和 UC_APP_KEY"
            )
        result = await uc_sdk.verify_token(token)
        if not result.get("success"):
            return None
        return {
            "user_id": str(result["user_id"]),
            "app_id": result.get("app_id"),
            "role": result.get("role", "user"),
            "vip_level": result.get("vip_level", 0),
        }

    async def get_current_user_id_optional(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> str | None:
        if credentials is None:
            return None
        result = await _validate_token(credentials.credentials)
        if result is None:
            return None
        return result.get("user_id")

    async def get_current_user_id_required(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> str:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        result = await _validate_token(credentials.credentials)
        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return result.get("user_id")

    async def get_current_user_full(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> dict:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        result = await _validate_token(credentials.credentials)
        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return result

    return get_current_user_id_optional, get_current_user_id_required, get_current_user_full