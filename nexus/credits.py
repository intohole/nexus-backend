"""平台积分钱包统一接入层：动作级计费 + LLM 网关自动计费 + 用量自动计量（全应用共用）。

- 计费：consume(feature) 按 billing_prices 定价扣积分，任何积分系统故障 fail-open 不阻塞业务；
  体验期(charge_mode=trial)余额不足不拦截，正式期(formal)返回 CreditsInsufficientError（NexusError 402，
  ErrorHandlerMiddleware 统一转 JSON 响应）。
- 网关自动计费：auto_charge_llm 挂在 LLMService 四路出口，调用成功后按平台价扣积分——
  未做显式动作计费的应用零改动建立扣费流水；显式计费的应用用 credits.auto_charge=false 关停防双重扣费。
- 计量：report_meter / report_llm_usage 把用户级 LLM 用量批量上报 usercenter（usage_meters），
  免费期即开始积累，作为定价校准与未来按量计费的数据底座。
- 用户归因：HTTP 请求内自动取 nexus.context 的 user_id（auth 中间件写入）；
  非HTTP上下文（后台任务/脚本）用 credits_user_scope(user_id) 显式声明，匿名调用自动跳过。
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from nexus.config import yaml_bool, yaml_get
from nexus.context import get_request_id, get_user_id
from nexus.errors import NexusError
from nexus.logging import get_logger
from nexus.llm_config import resolve_app_name

logger = get_logger("nexus.credits")

_user_scope_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_credits_user", default=""
)

METER_FLUSH_THRESHOLD = 20
METER_FLUSH_DELAY = 15.0
METER_BUFFER_MAX = 500

# LLM 网关自动计费：kind → 计费 feature 映射（embed 是基础设施型调用，默认不计费）
GATEWAY_KIND_FEATURE: dict[str, str] = {
    "chat": "chat",
    "chat_stream": "chat",
    "extract": "extract",
    "embed": "embed",
}
GATEWAY_KIND_DESC: dict[str, str] = {
    "chat": "AI 对话",
    "chat_stream": "AI 对话",
    "extract": "AI 抽取",
    "embed": "AI 向量",
}
DEFAULT_AUTO_CHARGE_KINDS = "chat,chat_stream,extract"
CHARGE_MODE_CACHE_TTL = 300.0


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


class CreditsService:
    def __init__(self) -> None:
        self._buffer: deque = deque(maxlen=METER_BUFFER_MAX)
        self._flush_task: Optional[asyncio.Task] = None
        self._meter_seq = 0
        self._charge_seq = 0
        self._charge_mode_cache: tuple[str, float] = ("", 0.0)

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

    async def consume(
        self,
        feature: str,
        user_id: int | str | None = None,
        ref_id: str | None = None,
        description: str | None = None,
        app_key: str | None = None,
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
            )
        except Exception as exc:
            logger.warning("积分消费调用失败，放行: %s", exc)
            return ConsumeResult(allowed=True, reason="credits_unavailable")
        if res.get("success") is False:
            message = str(res.get("message") or "")
            if "余额不足" in message:
                return ConsumeResult(
                    allowed=False, reason="insufficient_balance", raw=res
                )
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
    ) -> PrecheckResult:
        """动作前检查（定价+余额）：trial/off 恒放行；formal 余额不足 allowed=False。

        任何积分系统故障 fail-open 放行，不阻塞业务主流程。
        """
        uid = str(user_id or _current_user_id() or "")
        if not uid:
            return PrecheckResult(allowed=True, reason="no_user_context")
        sdk = self._sdk()
        if sdk is None:
            return PrecheckResult(allowed=True, reason="credits_disabled")
        try:
            res = await sdk.billing_precheck(
                user_id=int(uid), app_key=app_key or resolve_app_name(), feature=feature
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

    def _remember_charge_mode(self, mode: str) -> None:
        if mode:
            self._charge_mode_cache = (mode, time.monotonic())

    def _cached_charge_mode(self) -> str:
        mode, ts = self._charge_mode_cache
        if mode and time.monotonic() - ts < CHARGE_MODE_CACHE_TTL:
            return mode
        return ""

    def _next_charge_seq(self) -> int:
        self._charge_seq += 1
        return self._charge_seq

    @staticmethod
    def _auto_charge_kinds() -> set[str]:
        raw = yaml_get("credits", "auto_charge_kinds", DEFAULT_AUTO_CHARGE_KINDS)
        return {k.strip() for k in raw.split(",") if k.strip()}

    @classmethod
    def gateway_feature(cls, kind: str) -> str:
        """LLM kind → 计费 feature；网关计费关停（credits.auto_charge=false）返回空。"""
        feature = GATEWAY_KIND_FEATURE.get(kind, "")
        if not feature:
            return ""
        if not yaml_bool("credits", "auto_charge", True):
            return ""
        if kind not in cls._auto_charge_kinds():
            return ""
        return feature

    async def auto_charge_llm(self, *, kind: str, app_name: str, request_id: str) -> None:
        """LLM 网关出口自动计费：调用成功后按平台价扣积分，任何故障静默放行（绝不抛错）。

        ref_id 幂等防 HTTP 重试双扣；显式动作计费的应用用 credits.auto_charge=false 关停。
        """
        feature = self.gateway_feature(kind)
        if not feature:
            return
        try:
            outcome = await self.consume(
                feature,
                ref_id=f"llm:{request_id}:{self._next_charge_seq()}"[:100],
                description=GATEWAY_KIND_DESC.get(kind, ""),
                app_key=app_name,
            )
            if not outcome.allowed:
                logger.warning(
                    "LLM 网关计费余额不足(trial 透支/formal 已由前置预检拦截): kind=%s app=%s",
                    kind, app_name,
                )
        except Exception as exc:
            logger.warning("LLM 网关计费失败(放行): %s", exc)

    async def gateway_preflight(self, kind: str) -> None:
        """正式计费模式下 LLM 调用前预检，余额不足抛 CreditsInsufficientError（402）。

        仅当缓存的 charge_mode=formal 才发起预检请求——体验期/免费期零额外开销、零行为变化。
        """
        if self._cached_charge_mode() != "formal":
            return
        feature = self.gateway_feature(kind)
        if not feature:
            return
        result = await self.precheck(feature)
        if not result.allowed:
            raise CreditsInsufficientError("积分余额不足，请充值后重试")

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
        uid = str(user_id or _current_user_id() or "")
        if not uid:
            return
        self._meter_seq += 1
        rid = request_id or get_request_id() or uuid.uuid4().hex
        self._buffer.append({
            "user_id": int(uid),
            "app": app_key or resolve_app_name(),
            "feature": feature or "",
            "kind": kind or "chat",
            "model": model or "",
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "calls": int(calls or 1),
            "duration_ms": int(duration_ms or 0),
            "meter_key": f"{rid}:{self._meter_seq}"[:100],
        })
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = 0.1 if len(self._buffer) >= METER_FLUSH_THRESHOLD else METER_FLUSH_DELAY
        self._flush_task = loop.create_task(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush_meters()

    async def flush_meters(self) -> None:
        if not self._buffer:
            return
        items = list(self._buffer)
        self._buffer.clear()
        sdk = self._sdk()
        if sdk is None:
            return
        try:
            res = await sdk.billing_report_meters(items)
            if res.get("success") is False:
                self._requeue(items)
                logger.warning("计量上报被拒绝，已回队重试: %s", res.get("message"))
        except Exception as exc:
            self._requeue(items)
            logger.warning("计量上报失败，已回队重试: %s", exc)

    def _requeue(self, items: list) -> None:
        for item in reversed(items):
            self._buffer.appendleft(item)


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


async def report_llm_usage(
    *,
    app_name: str,
    kind: str,
    request_id: str,
    response: Any = None,
    latency_s: float = 0.0,
    calls: int = 1,
    feature: str = "",
) -> None:
    """LLM 调用出口计量钩子（永不抛错，匿名调用自动跳过）。"""
    try:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        if not input_tokens and not output_tokens:
            input_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        get_credits_service().report_meter(
            kind=kind,
            feature=feature,
            model=str(getattr(response, "model", "") or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            calls=calls,
            duration_ms=int(latency_s * 1000),
            app_key=app_name,
            request_id=request_id,
        )
    except Exception:
        pass
