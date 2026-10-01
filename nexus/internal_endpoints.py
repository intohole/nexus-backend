"""内部端点与健康检查：服务令牌校验、/api/_internal/* 监控端点、/health 与 /readiness。"""
from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from nexus.config import NexusConfig
from nexus.utils import HealthRegistry


async def require_service_token(request: Request) -> None:
    """内部端点校验：接受 UC 签发的服务 JWT（验签）或过渡期静态 SERVICE_TOKEN。"""
    from fastapi import HTTPException

    supplied: str = request.headers.get("X-Service-Token", "")
    if not supplied:
        from nexus.auth import extract_bearer_token

        supplied = extract_bearer_token(request.headers.get("Authorization")) or ""
    if not supplied:
        raise HTTPException(status_code=401, detail="Invalid service token")

    from nexus.middleware_auth import build_default_verifier

    verifier = build_default_verifier()
    if verifier.configured and await verifier.verify_service(supplied):
        return

    expected: str = os.environ.get("SERVICE_TOKEN", "")
    if expected and hmac.compare_digest(supplied, expected):
        return

    raise HTTPException(status_code=401, detail="Invalid service token")


def register_internal_endpoints(app: FastAPI) -> None:
    """A4: 在任意 FastAPI app 上注册 /api/_internal/* 端点。

    适用于不使用 nexus.create_app() 但仍需暴露内部监控端点的应用。
    端点强制要求服务令牌（require_service_token），匿名请求一律 401。
    """
    @app.post("/api/_internal/reload-llm", dependencies=[Depends(require_service_token)])
    async def reload_llm(request: Request) -> JSONResponse:
        """P0: 触发 ironman 配置热重载（内部端点）。

        lion 配置变更后，调用此端点立即重置 ironman Bootstrap，
        下次 LLM 调用时会用新配置重建。无需重启应用。
        """
        try:
            from nexus.ironman import reload_ironman
            await reload_ironman()
            return JSONResponse(
                content={"status": "ok", "message": "ironman Bootstrap reloaded"}
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": str(exc)},
            )

    @app.get("/api/_internal/llm-metrics", dependencies=[Depends(require_service_token)])
    async def llm_metrics_endpoint(request: Request) -> JSONResponse:
        """A4.1: 暴露 LLM 调用 metrics（内部端点）。

        返回 latency / tokens / error 维度的聚合指标，按 app/model 分解。
        供 miniDeploy 监控聚合或人工排查 LLM 调用健康度。
        """
        from nexus.llm_metrics import get_llm_metrics
        return JSONResponse(content=get_llm_metrics().snapshot())

    @app.get("/api/_internal/llm-circuit", dependencies=[Depends(require_service_token)])
    async def llm_circuit_endpoint(request: Request) -> JSONResponse:
        """A4.2: 暴露 LLM 熔断器状态（内部端点）。

        返回熔断器当前状态（closed/open/half_open）+ 连续失败/成功计数 + 配置。
        供监控告警：state=open 时说明 prompt-manager 网关故障。
        """
        from nexus.circuit_breaker import get_llm_circuit
        return JSONResponse(content=get_llm_circuit().to_dict())


def setup_health_check(
    app: FastAPI,
    registry: HealthRegistry,
    config: NexusConfig,
) -> None:
    @app.get("/health")
    async def health_check() -> dict[str, object]:
        return {
            "status": "healthy",
            "app": config.app_name,
            "version": config.app_version,
        }

    @app.get("/readiness")
    async def readiness_check() -> JSONResponse:
        checks: dict[str, bool] = await registry.run_all()
        all_healthy: bool = all(checks.values()) if checks else True
        status_code: int = 200 if all_healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if all_healthy else "not ready",
                "checks": checks,
            },
        )

    register_internal_endpoints(app)
