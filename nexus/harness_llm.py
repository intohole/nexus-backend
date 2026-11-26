from __future__ import annotations

from collections.abc import Awaitable, Callable

from ironman.types import LLMRequest, LLMResponse

LLMExecutor = Callable[[LLMRequest], Awaitable[LLMResponse]]


class HarnessGatewayError(RuntimeError):
    pass


def ensure_gateway_route(app_name: str) -> None:
    from ironman.bootstrap import current_app_name

    from nexus.ironman import is_ironman_available
    from nexus.ironman_config import is_gateway_mode

    if not is_ironman_available():
        raise HarnessGatewayError(
            f"AgentHarness({app_name}) 未初始化：请先 nexus.startup/ensure_ironman，"
            "禁止回落到 ironman 自持 provider 直连"
        )
    if not is_gateway_mode():
        raise HarnessGatewayError(
            f"AgentHarness({app_name}) 当前不是网关模式：Lion chat.gateway 未生效，"
            "直连会失去网关观测/身份/限流"
        )
    active = current_app_name()
    if active != app_name:
        raise HarnessGatewayError(
            f"AgentHarness({app_name}) 与 ironman Bootstrap app_name={active or '(empty)'} 不一致："
            "X-App-Id 身份归因将失真"
        )


def gateway_executor(app_name: str) -> LLMExecutor:
    ensure_gateway_route(app_name)

    async def _execute(request: LLMRequest) -> LLMResponse:
        # 长生命周期 executor 每次调用复核：bootstrap 换 app / 网关模式翻转后身份门不失效（纯内存检查）
        ensure_gateway_route(app_name)
        from ironman._facade import _execute

        return await _execute(request)

    return _execute


__all__ = ["HarnessGatewayError", "LLMExecutor", "ensure_gateway_route", "gateway_executor"]
