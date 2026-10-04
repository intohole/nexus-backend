"""平台积分钱包统一接入层：动作级计费 + LLM 网关自动计费 + 用量自动计量（全应用共用）。

- 计费：consume(feature) 按 billing_prices 定价扣积分，任何积分系统故障 fail-open 不阻塞业务；
  体验期(charge_mode=trial)余额不足不拦截，正式期(formal)返回 CreditsInsufficientError（NexusError 402，
  ErrorHandlerMiddleware 统一转 JSON 响应）。
- 网关自动计费（credits_gateway.GatewayBillingMixin）：auto_charge_llm 挂在 LLMService 四路出口，
  调用成功后按平台价×模型档位系数扣积分——未显式接入的应用零改动建立扣费流水。
- 预检：gateway_preflight 在 trial/formal 模式前置拦截（fair-use/余额不足），allowed 结果进程内缓存
  60s 降低 RPC（拒绝结果不缓存，日赠到账后立即可重试）；最终权威始终在 UC consume 侧。
- 计量：MeteringBuffer（credits_metering）把用户级 LLM 用量批量上报 usercenter（usage_meters）。
- 用户归因：HTTP 请求内自动取 nexus.context 的 user_id（auth 中间件写入）；
  非HTTP上下文（后台任务/脚本）用 credits_user_scope(user_id) 显式声明，匿名调用自动跳过。
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from nexus.context import get_request_id, get_user_id
from nexus.credits_gateway import (  # noqa: F401 (再导出)
    DEFAULT_AUTO_CHARGE_KINDS,
    GATEWAY_KIND_DESC,
    GATEWAY_KIND_FEATURE,
    GatewayBillingMixin,
)
from nexus.credits_metering import MeteringBuffer, report_llm_usage  # noqa: F401 (再导出)
from nexus.errors import NexusError
from nexus.logging import get_logger
from nexus.llm_config import resolve_app_name

logger = get_logger("nexus.credits")

_user_scope_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_credits_user", default=""
)

CHARGE_MODE_CACHE_TTL = 300.0
PRECHECK_CACHE_TTL = 60.0
PRECHECK_CACHE_MAX = 1000


class CreditsInsufficientError(NexusError):
    """正式计费模式下余额不足（NexusError 402，全局异常处理自动转 JSON）。"""

    status_code = 402
    error_code = "INSUFFICIENT_CREDITS"


@dataclass
class PrecheckResult:
    allowed: bool
    cost: Optional[int] = None
    balance: Optional[int] = None
    charge_mode: str = ""
    reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ConsumeResult:
    allowed: bool
    charged: bool = False
    balance: Optional[int] = None
    charge_mode: str = ""
    reason: str = ""
    raw: dict = field(default_factory=dict)


@contextlib.contextmanager
def credits_user_scope(user_id: int | str) -> Iterator[None]:
    """非 HTTP 上下文显式声明计费/计量用户（后台任务、脚本）。"""
    token = _user_scope_var.set(str(user_id))
    try:
        yield
    finally:
        _user_scope_var.reset(token)


def _current_user_id() -> str:
    return str(_user_scope_var.get() or get_user_id() or "")


def get_credits_service() -> "CreditsService":
    global _credits_service
    if _credits_service is None:
        _credits_service = CreditsService()
    return _credits_service


_credits_service: Optional["CreditsService"] = None


class CreditsService(GatewayBillingMixin):
    def __init__(self) -> None:
        self._charge_seq = 0
        self._charge_mode_cache: tuple[str, float] = ("", 0.0)
        self._precheck_cache: dict[tuple, tuple[float, PrecheckResult]] = {}
        self._meters = MeteringBuffer(self._report_meter_items, resolve_user=_current_user_id)

    @staticmethod
    def _sdk():
        try:
            from nexus.uc_sdk_helper import get_uc_sdk
            return get_uc_sdk()
        except Exception:
            return None

    @staticmethod
    def _make_ref_id(feature: str) -> str:
        rid = get_request_id() or uuid.uuid4().hex
        return f"{rid}:{feature}"[:100]

    def _next_charge_seq(self) -> int:
        self._charge_seq += 1
        return self._charge_seq

    def _remember_charge_mode(self, mode: str) -> None:
        if mode:
            self._charge_mode_cache = (mode, time.monotonic())

    def _cached_charge_mode(self) -> str:
        mode, ts = self._charge_mode_cache
        if mode and time.monotonic() - ts < CHARGE_MODE_CACHE_TTL:
            return mode
        return ""

    async def consume(
        self,
        feature: str,
        user_id: int | str | None = None,
        ref_id: str | None = None,
        description: str | None = None,
        app_key: str | None = None,
        cost_factor: float = 1.0,
    ) -> ConsumeResult:
        uid = str(user_id or _current_user_id() or "")
        if not uid:
            return ConsumeResult(allowed=True, reason="no_user_context")
        sdk = self._sdk()
        if sdk is None:
            return ConsumeResult(allowed=True, reason="credits_disabled")
        try:
            res = await sdk.billing_consume(
                user_id=int(uid),
                app_key=app_key or resolve_app_name(),
                feature=feature,
                ref_id=ref_id or self._make_ref_id(feature),
                description=description,
                cost_factor=cost_factor,
            )
        except Exception as exc:
            logger.warning("积分消费调用失败，放行: %s", exc)
            return ConsumeResult(allowed=True, reason="credits_unavailable")
        if res.get("success") is False:
            message = str(res.get("message") or "")
            if "额度已用完" in message:
                return ConsumeResult(allowed=False, reason="overdraft_limit", raw=res)
            if "余额不足" in message:
                return ConsumeResult(allowed=False, reason="insufficient_balance", raw=res)
            logger.warning("积分消费异常(放行): %s", message)
            return ConsumeResult(allowed=True, reason="credits_unavailable")
        data = res.get("data") or {}
        self._remember_charge_mode(str(data.get("charge_mode") or ""))
        return ConsumeResult(
            allowed=True,
            charged=bool(data.get("charged")),
            balance=data.get("balance"),
            charge_mode=str(data.get("charge_mode") or ""),
            reason="ok",
            raw=data,
        )

    async def precheck(
        self,
        feature: str,
        user_id: int | str | None = None,
        app_key: str | None = None,
        cost_factor: float = 1.0,
    ) -> PrecheckResult:
        """动作前检查（定价+余额）：trial/off 恒放行；formal 余额不足 allowed=False。

        任何积分系统故障 fail-open 放行；allowed 结果进程内缓存 60s（拒绝不缓存，
        领取每日赠送后立即可重试），规模化时把 trial 预检 RPC 降为 1 次/分钟/用户。
        """
        uid = str(user_id or _current_user_id() or "")
        if not uid:
            return PrecheckResult(allowed=True, reason="no_user_context")
        key = (uid, feature, round(float(cost_factor), 3))
        cached = self._precheck_cache.get(key)
        if cached and time.monotonic() - cached[0] < PRECHECK_CACHE_TTL:
            return cached[1]
        result = await self._precheck_remote(feature, uid, app_key, cost_factor)
        if result.allowed and result.reason == "ok":
            self._cache_precheck(key, result)
        return result

    async def _precheck_remote(
        self, feature: str, uid: str, app_key: str | None, cost_factor: float
    ) -> PrecheckResult:
        sdk = self._sdk()
        if sdk is None:
            return PrecheckResult(allowed=True, reason="credits_disabled")
        try:
            res = await sdk.billing_precheck(
                user_id=int(uid), app_key=app_key or resolve_app_name(),
                feature=feature, cost_factor=cost_factor,
            )
        except Exception as exc:
            logger.warning("积分预检调用失败，放行: %s", exc)
            return PrecheckResult(allowed=True, reason="credits_unavailable")
        if res.get("success") is False:
            logger.warning("积分预检异常(放行): %s", res.get("message"))
            return PrecheckResult(allowed=True, reason="credits_unavailable")
        data = res.get("data") or {}
        return PrecheckResult(
            allowed=bool(data.get("allowed", True)),
            cost=data.get("cost"),
            balance=data.get("balance"),
            charge_mode=str(data.get("charge_mode") or ""),
            reason=str(data.get("reason") or "ok"),
            raw=data,
        )

    def _cache_precheck(self, key: tuple, result: PrecheckResult) -> None:
        if len(self._precheck_cache) >= PRECHECK_CACHE_MAX:
            now = time.monotonic()
            self._precheck_cache = {
                k: v for k, v in self._precheck_cache.items()
                if now - v[0] < PRECHECK_CACHE_TTL
            }
            if len(self._precheck_cache) >= PRECHECK_CACHE_MAX:
                self._precheck_cache.clear()
        self._precheck_cache[key] = (time.monotonic(), result)

    def report_meter(
        self,
        kind: str,
        feature: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
        duration_ms: int = 0,
        user_id: int | str | None = None,
        app_key: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self._meters.append(
            kind=kind, feature=feature, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            calls=calls, duration_ms=duration_ms,
            user_id=user_id, app_key=app_key, request_id=request_id,
        )

    async def flush_meters(self) -> None:
        await self._meters.flush()

    async def _report_meter_items(self, items: list) -> dict:
        sdk = self._sdk()
        if sdk is None:
            return {"success": False, "message": "credits_disabled"}
        return await sdk.billing_report_meters(items)


def charged(feature: str, description: str | None = None) -> Callable:
    """动作级计费装饰器：动作成功完成后扣积分（fail-open，积分故障不阻塞业务）。

    正式计费模式下余额不足抛 CreditsInsufficientError，应用层决定拦截文案。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            outcome = await get_credits_service().consume(feature, description=description)
            if not outcome.allowed:
                raise CreditsInsufficientError("积分余额不足")
            return result
        return wrapper
    return decorator
