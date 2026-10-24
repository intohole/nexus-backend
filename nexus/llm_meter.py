"""LLM 网关计费三助手：计量、预检、自动计费（自 nexus.llm 拆出，消文件超限）。

fail-open 契约：预检仅 CreditsInsufficientError(402) 放行为异常，其余积分故障
静默；计费绝不影响响应（r12 计费正确性闭环的调用面契约）。
"""
from __future__ import annotations

import time

from nexus.credits import (
    CreditsInsufficientError,
    get_credits_service,
    report_llm_usage,
)


async def meter(kind: str, app_name: str, request_id: str, response: object = None,
                start: float = 0.0, calls: int = 1, feature: str = "") -> None:
    await report_llm_usage(app_name=app_name, kind=kind, request_id=request_id, response=response,
                           latency_s=(time.monotonic() - start) if start else 0.0, calls=calls,
                           feature=feature)


async def preflight(kind: str, model: str = "") -> None:
    """正式计费模式下调用前预检；CreditsInsufficientError(402) 放行，其余积分故障静默。"""
    try:
        await get_credits_service().gateway_preflight(kind, model=model)
    except CreditsInsufficientError:
        raise
    except Exception:
        pass


async def charge(kind: str, app_name: str, request_id: str, model: str = "") -> None:
    """调用成功后网关自动计费（fail-open，绝不影响响应），按模型档位系数定价。"""
    try:
        await get_credits_service().auto_charge_llm(
            kind=kind, app_name=app_name, request_id=request_id, model=model
        )
    except Exception:
        pass
