"""LLM 统一接入层核心：单例、网关路由前置、确定性缓存与主链 chat."""
from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Optional

from nexus.context import get_request_id
from nexus.logging import get_logger

if TYPE_CHECKING:
    from nexus.llm import LLMService

from nexus.circuit_breaker import get_llm_circuit, llm_provider_key, record_llm_outcome
from nexus.credits import credits_gateway_handled
from nexus.llm_budget import (
    DEFAULT_THINKING_BUDGET,
    OutputMode,
    canonicalize_task_type,
    resolve_effective_budget,
)
from nexus.llm_cache import PromptCache, get_prompt_cache
from nexus.llm_config import configure_ironman, effective_retries, resolve_app_name
from nexus.llm_helpers import (
    apply_output_discipline,
    convert_messages,
    extract_content,
    record_usage,
    resolve_namespace,
)
from nexus.llm_meter import charge as _charge
from nexus.llm_meter import meter as _meter
from nexus.llm_meter import preflight as _preflight
from nexus.llm_metrics import llm_telemetry
from nexus.llm_utils import with_llm_retry

logger = get_logger("nexus.llm")

DEFAULT_MAX_OUTPUT_TOKENS: int = int(os.environ.get("LLM_DEFAULT_MAX_OUTPUT_TOKENS", "2048"))


class LLMCore:
    _instance: Optional["LLMService"] = None

    def __new__(cls) -> "LLMService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _build_extra(
        json_mode: bool,
        namespace: Optional[str],
        task_type: Optional[str],
        reasoning_effort: Optional[str] = None,
        extra_fields: Optional[dict[str, object]] = None,
    ) -> dict[str, object] | None:
        extra: dict[str, object] | None = None
        if json_mode:
            extra = {"response_format": {"type": "json_object"}}
        ns = resolve_namespace(namespace)
        if ns:
            extra = dict(extra or {})
            extra["namespace"] = ns
        if task_type:
            extra = dict(extra or {})
            extra["task_type"] = task_type
        if reasoning_effort:
            extra = dict(extra or {})
            extra["reasoning_effort"] = reasoning_effort
        if extra_fields:
            extra = dict(extra or {})
            extra.update(extra_fields)
        return extra

    async def _prepare_call(
        self,
        *,
        temperature: Optional[float],
        max_tokens: Optional[int],
        task_type: Optional[str],
        output_mode: Optional[OutputMode],
        json_mode: bool,
        namespace: Optional[str],
        model: Optional[str],
        enable_thinking: Optional[bool],
        thinking_budget: Optional[int],
        reasoning_effort: Optional[str] = None,
        no_cache: bool = False,
        extra_fields: Optional[dict[str, object]] = None,
    ) -> tuple[object, float, int, Optional[OutputMode]]:
        """公共前置：配置 ironman、预算解析并构造 LLMOptions。

        返回 (llm_opts, 生效温度, 生效 max_tokens, output_mode)。
        thinking 解析：显式传参恒优先，未传参吃任务档位；档位 True 时按档位
        thinking_budget 封顶（LLMOptions 层 True=发键生效/False=facade 坍缩为
        不表态，由网关兜底链决定）。
        no_cache 全层禁用：跳过本地 PromptCache 读写并透传上游——同 prompt
        契约失败重试（nexus.llm_utils.ask_json）依赖此语义绕开坏缓存复读。
        """
        task_type = canonicalize_task_type(task_type)
        await configure_ironman()
        try:
            from ironman.tracing.request_scope import set_outbound_request_id

            from nexus.trace import ensure_request_id

            set_outbound_request_id(ensure_request_id())
        except ImportError:
            pass
        budget_max, budget_temp, budget_mode, budget_thinking, budget_thinking_budget = (
            resolve_effective_budget(
                task_type, max_tokens, temperature, output_mode,
                enable_thinking, thinking_budget,
            )
        )
        temp = 0.7 if budget_temp is None else budget_temp
        eff_max: int = budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        eff_thinking: bool = budget_thinking if budget_thinking is not None else False
        eff_thinking_budget: int = (budget_thinking_budget
                                    if budget_thinking_budget is not None
                                    else DEFAULT_THINKING_BUDGET)
        from ironman.types import LLMOptions

        opts = LLMOptions(
            temperature=temp,
            max_tokens=eff_max,
            model=model,
            enable_thinking=eff_thinking,
            thinking_budget=eff_thinking_budget,
            no_cache=no_cache,
            extra=self._build_extra(json_mode, namespace, task_type, reasoning_effort, extra_fields),
        )
        return opts, temp, eff_max, budget_mode

    async def _execute(
        self,
        do,
        timeout: float,
        max_retries: int,
        app_name: str,
        request_id: str,
        kind: str,
        feature: str = "",
        llm_opts: object = None,
    ) -> tuple[str, object]:
        entry_key = llm_provider_key(llm_opts)
        entry_circuit = get_llm_circuit(entry_key)
        async with llm_telemetry(kind, app_name, request_id) as (metrics, start):
            async def _do_with_circuit() -> object:
                await entry_circuit.acquire_call()
                try:
                    response: object = await do()
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() > 0:
                        await record_llm_outcome(
                            entry_key, TimeoutError("LLM call cancelled (likely timeout)")
                        )
                    raise
                except Exception as e:
                    await record_llm_outcome(entry_key, e)
                    raise
                await record_llm_outcome(entry_key, None, response)
                extract_content(response, request_id)
                return response
            response: object = await with_llm_retry(
                _do_with_circuit, timeout, effective_retries(max_retries)
            )
            result: str = extract_content(response, request_id)
            record_usage(metrics, app_name, response, time.monotonic() - start, None)
            await _meter(kind, app_name, request_id, response, start, feature=feature)
            logger.info(
                "LLM %s completed [req_id=%s, app=%s, latency=%.2fs]",
                kind, request_id, app_name, time.monotonic() - start,
            )
            return result, response

    @staticmethod
    def _charge_usage(response: object) -> dict[str, object] | None:
        """从 ironman LLMResponse 提取按量计费原料（模型返回 usage；缓存命中 None）。"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        from datetime import datetime, timezone

        return {
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
            "called_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _usage_payload(result: str, response: object) -> dict[str, object]:
        """从 ironman LLMResponse 提取调用方可见的用量账单（缓存命中时 response 为 None）。

        finish_reason（r234，S180-GAP）：providers 已填（OpenAI 兼容响应
        choices[0].finish_reason），账单面透传供 budget_retry 分型——缓存命中
        response=None 或字段缺席时为空串（无截断语义）。
        """
        usage = getattr(response, "usage", None)
        return {
            "content": result,
            "model": getattr(response, "model", "") or "",
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
            "cost_usd": float(getattr(response, "cost_usd", 0.0) or 0.0),
            "latency_ms": int(float(getattr(response, "latency_ms", 0.0) or 0.0)),
            "provider": getattr(response, "provider", "") or "",
            "cached": bool(getattr(response, "cached", False)) or response is None,
            "finish_reason": str(getattr(response, "finish_reason", "") or ""),
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        json_mode: bool = False,
        concise: bool = False,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        feature: str = "",
        no_cache: bool = False,
        images: Optional[list[str]] = None,
        vision_meta: Optional[dict[str, object]] = None,
        tools: Optional[list[object]] = None,
        _want_usage: bool = False,
    ) -> str | dict[str, object]:
        """r263 刀4 判定书定档=收编（内部组合原语底座）：本方法是 ask/chat_with_usage
        （llm_core.py:306/300）与 ask_json 计费孪生 _billing_chat（llm.py:105）及
        LLMJsonClient 五方法（llm_client.py:80/132/203/291/314）的底座——包内底座
        调用共 8 处（r263 终局找茬 P2-1 锚点勘误：原「7 处」系 def 行凑数+ask 真锚
        306 漏记，终审实拍 grep self.chat/_svc.chat 全包=8）；业务消费走组合原语面；
        业务仓裸 chat 跨包消费=0（r263 分层普查实锚，退役窗触发器=chat 私有化
        _chat+底座迁移评估，M8 别名一周期节奏）。外部接入请走 ask/chat_with_usage。

        images（r30 additive）：data URL/URL 列表，挂到末条 user 消息尾部
        （convert_messages 位置契约）；带图调用恒绕 PromptCache——缓存 key 只拼
        消息文本，同 prompt 不同截图会命中旧答案串图（视觉修复环每轮换图=必须 bypass）。
        tools（r259 additive）：ironman ToolDef 列表直透（r259 勘察 P0#1 断点修复：
        chat 签名此前无 tools，nexus 单轮面承载不了工具调用）；带 tools 同判例恒绕
        PromptCache——key 不含 tools 且缓存值无法表达 tool_calls 轮。
        tools×json_mode 互斥（r260 刀I 加固）：response_format 与 function calling
        在 deepseek wire 上组合行为未定义，主环/judge/抽取三面均无此组合，命中即拦。"""
        if tools and json_mode:
            raise ValueError("tools 与 json_mode 互斥：json_object 响应格式与 function calling 组合在网关侧行为未定义")
        opts, temp, eff_max, budget_mode = await self._prepare_call(
            temperature=temperature, max_tokens=max_tokens, task_type=task_type,
            output_mode=output_mode, json_mode=json_mode, namespace=namespace,
            model=model, enable_thinking=enable_thinking, thinking_budget=thinking_budget,
            reasoning_effort=reasoning_effort, no_cache=no_cache,
            extra_fields={"vision_meta": vision_meta} if vision_meta else None,
        )
        system, _ = apply_output_discipline(system, "", concise, json_mode, budget_mode)
        ironman_messages = convert_messages(messages, system, images)
        cache = None if (no_cache or images or tools) else (get_prompt_cache() if temp <= 0.0 else None)
        if cache is not None:
            key: str = PromptCache.make_messages_key(
                system, messages, temp, eff_max,
                model=str(getattr(opts, "model", "") or ""),
                namespace=resolve_namespace(namespace),
                enable_thinking=bool(getattr(opts, "enable_thinking", False)),
            )
            hit: Optional[str] = cache.get(key)
            if hit is not None:
                return self._usage_payload(hit, None) if _want_usage else hit
        from ironman import chat as _chat
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        await _preflight("chat", model=str(getattr(opts, "model", "") or ""))

        async def _do() -> object:
            with credits_gateway_handled():
                return await _chat(messages=ironman_messages, llm=opts, tools=tools)

        result, response = await self._execute(_do, timeout, max_retries, app_name, request_id, "chat", feature=feature, llm_opts=opts)
        await _charge("chat", app_name, request_id, model=str(getattr(response, "model", "") or ""),
                      usage=self._charge_usage(response))
        if cache is not None and result:
            cache.set(key, result)
        return self._usage_payload(result, response) if _want_usage else result

    async def chat_with_usage(self, messages: list[dict[str, str]], **kwargs) -> dict[str, object]:
        """chat 的带账单版：返回 {content, model, total_tokens, cached_tokens, cost_usd, latency_ms, provider, cached, finish_reason}。

        计费/熔断/重试/缓存/json_mode 语义与 chat 完全一致；缓存命中时 usage 为 0 且 cached=True。
        """
        kwargs["_want_usage"] = True
        result = await self.chat(messages=messages, **kwargs)
        assert isinstance(result, dict)
        return result

    async def ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        """单轮提问 = 单条 user 消息的 chat。"""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        )
