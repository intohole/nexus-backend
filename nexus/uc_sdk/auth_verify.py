"""UC SDK 令牌验证层：JWKS→本地 HS256→远程 三层验证调度、jti 黑名单同步与 org 声明复核。"""
import logging
import time
from typing import Dict, Any

from nexus.uc_sdk.resilience import OrgRecheckCache

try:
    from jose import JWTError
except ImportError:
    JWTError = Exception

logger = logging.getLogger(__name__)


class TokenVerifyMixin:

    _ORG_RECHECK_TTL_S: float = 60.0
    _ORG_RECHECK_TIMEOUT_S: float = 3.0

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
                # 签名错误不即时刷钥：垃圾签名洪流逐请求打 UC JWKS=放大 DoS
                #（与 middleware_auth 未知 kid 限频同纪律，60s 内只刷一次）
                if time.time() - getattr(self, "_jwks_fetched_at", 0.0) > 60.0:
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
            "org_role": payload.get("org_role"),
            "vip_level": payload.get("vip_level"),
            "display_name": payload.get("display_name", ""),
            "has_permission": True,
        }

        result = await self._apply_org_recheck(token, payload, result)

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
            "org_role": payload.get("org_role"),
            "vip_level": payload.get("vip_level"),
            "display_name": payload.get("display_name", ""),
            "has_permission": True
        }

        result = await self._apply_org_recheck(token, payload, result)

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

    def _get_org_recheck_cache(self) -> OrgRecheckCache:
        cache = getattr(self, "_org_recheck_cache", None)
        if cache is None:
            cache = OrgRecheckCache(ttl=self._ORG_RECHECK_TTL_S)
            self._org_recheck_cache = cache
        return cache

    async def _apply_org_recheck(self, token: str, payload: Dict[str, Any],
                                 result: Dict[str, Any]) -> Dict[str, Any]:
        """本地验签路径的 org 声明低频复核（UC r20 遗留①收口）。

        access TTL 内 token 携带的 org_id/org_role 可能已漂移（成员被移出/
        降级/换组），远程兜底路径已被 UC r20 校正为 DB 实时派生，本地两层
        仍透传声明。组织态 token（带 org_id）每 _ORG_RECHECK_TTL_S 复核一次
        权威端点 /token/validate（复核专用短超时 _ORG_RECHECK_TIMEOUT_S，
        UC 挂起不拖长业务请求），漂移即覆盖并打观测行；个人态 token 不复核
        （org 回落由 refresh 从 DB 重派生自愈，r18 设计）。fail-open：UC
        不可达或应答形状漂移时保留本地声明并退避，不拖垮认证主链、不打爆
        validate 端点。
        """
        if not result.get("org_id"):
            return result
        jti = payload.get("jti")
        if not jti:
            return result
        jti = str(jti)
        cache = self._get_org_recheck_cache()
        if not cache.needs_recheck(jti):
            return result
        cache.mark_rechecked(jti)
        try:
            remote = await self._verify_token_remote(token, timeout=self._ORG_RECHECK_TIMEOUT_S)
        except Exception as exc:
            logger.warning("org 复核失败(fail-open 保留本地声明): jti=%s err=%s", jti, exc)
            return result
        if not isinstance(remote, dict) or remote.get("success") is not True:
            return result
        new_org = remote.get("org_id")
        new_role = remote.get("org_role")
        old_org = result.get("org_id")
        old_role = result.get("org_role")
        if (new_org or None) != (old_org or None) or (new_role or None) != (old_role or None):
            logger.info(
                "org 本地声明已复核校正: user=%s local_org=(%s,%s) current_org=(%s,%s)",
                result.get("user_id"), old_org, old_role, new_org, new_role,
            )
        result["org_id"] = new_org
        result["org_role"] = new_role
        return result

    async def _verify_token_remote(self, token: str, permission: str = None,
                                   timeout: float = None) -> Dict[str, Any]:
        data = {"token": token}
        if permission:
            data["permission"] = permission
        kwargs: Dict[str, Any] = {"json": data}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = await self._request("POST", "/api/auth/token/validate", **kwargs)
        if result.get("success") and result.get("data"):
            inner = result["data"]
            return {
                "success": True,
                "user_id": inner.get("user_id"),
                "app_id": inner.get("app_id"),
                "role": inner.get("role"),
                "org_id": inner.get("org_id"),
                "org_role": inner.get("org_role"),
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
