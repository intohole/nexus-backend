"""UC SDK 令牌验证层：JWKS→本地 HS256→远程 三层验证调度与 jti 黑名单同步。"""
from typing import Dict, Any

try:
    from jose import JWTError
except ImportError:
    JWTError = Exception


class TokenVerifyMixin:

    def _credential_guard(self) -> Dict[str, Any] | None:
        if self.app_key:
            return None
        return {
            "success": False,
            "detail": "认证服务未就绪（应用凭证未同步），请稍后重试",
            "status_code": 503,
        }

    async def verify_token(self, token: str = None, permission: str = None) -> Dict[str, Any]:
        use_token = token or self._access_token
        if not use_token:
            return {"success": False, "detail": "No token provided"}
        jwks_result = await self._verify_token_jwks(use_token, permission)
        if jwks_result is not None:
            return jwks_result
        if self.jwt_secret_key and self._jwt_available():
            local = await self._verify_token_local(use_token, permission)
            if local.get("success"):
                return local
        return await self._verify_token_remote(use_token, permission)

    async def _verify_token_jwks(self, token: str, permission: str = None) -> Dict[str, Any] | None:
        if not token:
            return None
        try:
            from jose import jwt as jose_jwt
            from jose import jwk
            header = jose_jwt.get_unverified_header(token)
            kid = header.get("kid")
            key_jwk = self._jwks_by_kid.get(kid)
            if key_jwk is None:
                await self._refresh_jwks()
                key_jwk = self._jwks_by_kid.get(kid)
            if key_jwk is None:
                return None
            public_key = jwk.construct(key_jwk)
            try:
                payload = jose_jwt.decode(
                    token, public_key, algorithms=["RS256"],
                    options={"verify_exp": False},
                )
            except JWTError:
                await self._refresh_jwks()
                key_jwk = self._jwks_by_kid.get(kid)
                if key_jwk is None:
                    return None
                try:
                    payload = jose_jwt.decode(
                        token, jwk.construct(key_jwk), algorithms=["RS256"],
                        options={"verify_exp": False},
                    )
                except JWTError:
                    return {"success": False, "detail": "Invalid token"}
                except Exception:
                    return None
        except Exception:
            return None

        from datetime import datetime, timezone
        exp = payload.get("exp")
        if exp and datetime.now(timezone.utc) > datetime.fromtimestamp(exp, tz=timezone.utc):
            return {"success": False, "detail": "Token expired"}

        jti = payload.get("jti")
        if jti:
            cached = await self._blacklist_cache.is_blacklisted(jti)
            if cached is True:
                return {"success": False, "detail": "Token has been revoked"}
            if cached is None and self._blacklist_cache.needs_sync():
                await self._sync_blacklist()

        result = {
            "success": True,
            "user_id": payload.get("sub"),
            "app_id": payload.get("app_id"),
            "role": payload.get("role"),
            "org_id": payload.get("org_id"),
            "vip_level": payload.get("vip_level"),
            "display_name": payload.get("display_name", ""),
            "has_permission": True,
        }

        if permission:
            try:
                perm_result = await self._request(
                    "POST", "/api/auth/check-permission",
                    json={"token": token, "permission": permission},
                )
                if perm_result.get("success"):
                    result["has_permission"] = perm_result.get("data", {}).get("has_permission", False)
                else:
                    result["has_permission"] = False
            except Exception:
                result["has_permission"] = None

        return result

    @staticmethod
    def _jwt_available() -> bool:
        try:
            __import__("jose.jwt")
            return True
        except ImportError:
            return False

    async def _verify_token_local(self, token: str, permission: str = None) -> Dict[str, Any]:
        from jose import jwt as jose_jwt
        try:
            payload = jose_jwt.decode(token, self.jwt_secret_key, algorithms=["HS256"])
        except JWTError:
            return {"success": False, "detail": "Invalid token"}
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"本地JWT解码异常，降级远程验证: {e}")
            return await self._verify_token_remote(token, permission)

        from datetime import datetime, timezone
        exp = payload.get("exp")
        if exp and datetime.now(timezone.utc) > datetime.fromtimestamp(exp, tz=timezone.utc):
            return {"success": False, "detail": "Token expired"}

        jti = payload.get("jti")
        if jti:
            cached = await self._blacklist_cache.is_blacklisted(jti)
            if cached is True:
                return {"success": False, "detail": "Token has been revoked"}
            if cached is None and self._blacklist_cache.needs_sync():
                await self._sync_blacklist()

        result = {
            "success": True,
            "user_id": payload.get("sub"),
            "app_id": payload.get("app_id"),
            "role": payload.get("role"),
            "org_id": payload.get("org_id"),
            "vip_level": payload.get("vip_level"),
            "display_name": payload.get("display_name", ""),
            "has_permission": True
        }

        if permission:
            try:
                perm_result = await self._request(
                    "POST", "/api/auth/check-permission",
                    json={"token": token, "permission": permission}
                )
                if perm_result.get("success"):
                    result["has_permission"] = perm_result.get("data", {}).get("has_permission", False)
                else:
                    result["has_permission"] = False
            except Exception:
                result["has_permission"] = None

        return result

    async def _verify_token_remote(self, token: str, permission: str = None) -> Dict[str, Any]:
        data = {"token": token}
        if permission:
            data["permission"] = permission
        result = await self._request("POST", "/api/auth/token/validate", json=data)
        if result.get("success") and result.get("data"):
            inner = result["data"]
            return {
                "success": True,
                "user_id": inner.get("user_id"),
                "app_id": inner.get("app_id"),
                "role": inner.get("role"),
                "org_id": inner.get("org_id"),
                "vip_level": inner.get("vip_level"),
                "username": inner.get("username", ""),
                "nickname": inner.get("nickname", ""),
                "display_name": inner.get("display_name", ""),
                "has_permission": inner.get("has_permission", True),
            }
        return result

    async def _sync_blacklist(self):
        try:
            since = self._blacklist_cache._last_sync_at
            result = await self._request(
                "GET", f"/api/auth/blacklist/recent?since={since}",
                skip_refresh=True
            )
            if result.get("success") and result.get("data"):
                for jti in result["data"].get("blacklisted_jtis", []):
                    await self._blacklist_cache.mark_blacklisted(jti)
            self._blacklist_cache.mark_synced()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"黑名单同步失败: {e}")

    async def sync_blacklist(self):
        await self._sync_blacklist()
