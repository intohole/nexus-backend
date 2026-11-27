from __future__ import annotations

from ironman.kernel import LLMExecutor
from ironman.memory.summarizer import SummaryCallOutcome, SummaryLLM
from ironman.types import LLMRequest, LLMResponse

LLMExecutor = LLMExecutor  # re-export：r260 B1 单源=ironman.kernel.ports（契约面不变）


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


def summary_llm_from_gateway(app_name: str) -> "SummaryLLM":
    """r263 刀1：宿主侧 SUMMARY 摘要闭包适配构件（nexus 网关→ironman SummaryCallOutcome）。

    普查判定（r263）：全舰队 SUMMARY 策略零生产消费（build_agent_harness 零生产调用、
    Bootstrap.create 不建 harness、ironman contracts §8 休眠面登记在案）——本构件
    **入库不接线**，激活触发器=B1 首接（首个宿主 harness 会话启用 SUMMARY 策略时
    以 summarizer=StructuredSummarizer(llm_call=summary_llm_from_gateway(app)) 形态
    直兑；r263 法官 R1 形状 c+登记前置）。语义：finish_reason∈{length,content_filter}
    → SummaryCallOutcome（ironman summarize() 据此拒落盘截断摘要，pi compaction.ts:590
    纪律）；缓存命中 finish_reason="" 恒 str 返回零破坏。身份门与 gateway_executor
    同律：构造+每调用双复核。
    """
    ensure_gateway_route(app_name)

    async def _summarize(prompt: str) -> "str | SummaryCallOutcome":
        ensure_gateway_route(app_name)
        from nexus.llm import get_llm_service

        bill = await get_llm_service().chat_with_usage(
            messages=[{"role": "user", "content": prompt}],
        )
        text = str(bill.get("content") or "")
        finish_reason = str(bill.get("finish_reason") or "")
        if finish_reason in ("length", "content_filter"):
            return SummaryCallOutcome(text=text, finish_reason=finish_reason)
        return text

    return _summarize


__all__ = [
    "HarnessGatewayError",
    "LLMExecutor",
    "ensure_gateway_route",
    "gateway_executor",
    "summary_llm_from_gateway",
]
