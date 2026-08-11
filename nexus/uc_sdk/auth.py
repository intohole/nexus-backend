import time
from typing import Dict, Any

from nexus.uc_sdk.pkce import PKCEHelper

try:
    from jose import JWTError
except ImportError:
    JWTError = Exception


class AuthMixin:
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
            "vip_level": payload.get("vip_level"),
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
            from jose import jwt
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
            "vip_level": payload.get("vip_level"),
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
                "vip_level": inner.get("vip_level"),
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

    async def login(self, username: str = None, email: str = None, phone: str = None,
                    password: str = None, invite_code: str = None) -> Dict[str, Any]:
        data = {"password": password, "app_key": self.app_key}
        if username:
            data["username"] = username
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        if invite_code:
            data["invite_code"] = invite_code
        result = await self._request("POST", "/api/auth/login", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def register(self, username: str = None, email: str = None, phone: str = None,
                       password: str = None, invite_code: str = None) -> Dict[str, Any]:
        data = {"password": password, "app_key": self.app_key}
        if username:
            data["username"] = username
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        if invite_code:
            data["invite_code"] = invite_code
        result = await self._request("POST", "/api/auth/register", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            client = await self._get_client()
            response = await client.post(
                "/api/auth/refresh",
                json={"refresh_token": self._refresh_token}
            )
            if response.status_code >= 400:
                self.clear_tokens()
                return False
            result = response.json()
            if result.get("success") and result.get("data"):
                self._set_tokens(result["data"])
                return True
        except Exception:
            self.clear_tokens()
        return False

    async def refresh_with_token(self, refresh_token: str) -> Dict[str, Any] | None:
        try:
            client = await self._get_client()
            response = await client.post(
                "/api/auth/refresh",
                json={"refresh_token": refresh_token}
            )
            if response.status_code >= 400:
                return None
            result = response.json()
            if result.get("success") and result.get("data"):
                data = result["data"]
                return {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token", refresh_token),
                    "token_type": data.get("token_type", "bearer"),
                    "expires_in": data.get("expires_in")
                }
        except Exception:
            pass
        return None

    async def logout(self, token: str = None) -> None:
        try:
            await self._request("POST", "/api/auth/logout", token=token, skip_refresh=True)
        except Exception:
            pass
        self.clear_tokens()

    async def client_credentials(self) -> Dict[str, Any]:
        if not self.app_secret:
            raise ValueError("app_secret is required for client_credentials")
        client = await self._get_client()
        response = await client.post(
            "/api/auth/token",
            json={
                "grant_type": "client_credentials",
                "app_key": self.app_key,
                "app_secret": self.app_secret
            }
        )
        response.raise_for_status()
        result = response.json()
        if result.get("success") and result.get("data"):
            self._access_token = result["data"].get("access_token")
            if result["data"].get("expires_in"):
                self._token_expires_at = time.time() + result["data"]["expires_in"]
        return result

    async def check_permission(self, token: str, permission: str) -> Dict[str, Any]:
        return await self._request(
            "POST", "/api/auth/check-permission",
            json={"token": token, "permission": permission}
        )

    def get_authorization_url(self, redirect_uri: str, state: str = None,
                               scope: str = None) -> Dict[str, str]:
        import secrets as _secrets
        code_verifier, code_challenge = PKCEHelper.generate()
        state = state or _secrets.token_urlsafe(32)
        self._pkce_state[state] = code_verifier

        params = {
            "client_id": self.app_key,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if scope:
            params["scope"] = scope

        query = "&".join(f"{k}={v}" for k, v in params.items())
        return {
            "url": f"{self.base_url}/api/auth/authorize?{query}",
            "state": state,
            "code_verifier": code_verifier,
        }

    async def exchange_authorization_code(self, code: str, redirect_uri: str,
                                           code_verifier: str = None) -> Dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.app_key,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        elif self.app_secret:
            data["client_secret"] = self.app_secret

        result = await self._request("POST", "/api/auth/token/exchange", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result