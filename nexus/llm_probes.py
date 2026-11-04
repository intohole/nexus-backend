"""LLM 探针与旁路调用族：probe_ping（业务探针 llm 问标准实现）、extract、embed."""
from __future__ import annotations

import time
from typing import Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.circuit_breaker import get_llm_circuit
from nexus.credits import credits_gateway_handled
from nexus.llm_meter import charge as _charge, meter as _meter, preflight as _preflight
from nexus.llm_utils import with_llm_retry
from nexus.llm_helpers import convert_messages, extract_content, record_usage
from nexus.llm_config import configure_ironman, effective_retries, resolve_app_name
from nexus.llm_metrics import llm_telemetry

logger = get_logger("nexus.llm")


class LLMProbesMixin:
    async def probe_ping(self, prompt: str = "ping", timeout: float = 20.0) -> dict[str, object]:
        """探针专用 ping（业务探针契约 llm 问的标准实现），不抛异常。

        与 ask() 的三点差异：no_cache 旁路响应缓存（缓存命中=探针失明，永不
        感知真实故障）；content 或 reasoning 非空均判链路活着（推理模型
        max_tokens 被 reasoning 耗尽时 content 为空是预算伪影，不是链路故障，
        DeepSeek-R1 族教科书坑）；零重试（探针要即时真相，重试会放大故障期流量）。
        返回 {ok, reply_head, reasoning_only, latency_ms, model, error}。
        """
        await configure_ironman()
        from ironman import chat as _chat
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        await _preflight("chat")
        opts = LLMOptions(max_tokens=16, temperature=0.7, no_cache=True)
        ironman_messages = convert_messages([{"role": "user", "content": prompt}], None)
        circuit = get_llm_circuit()
        started: float = time.monotonic()

        async def _do() -> object:
            with credits_gateway_handled():
                return await _chat(messages=ironman_messages, llm=opts)

        try:
            async with llm_telemetry("chat", app_name, request_id) as (metrics, start):
                async def _do_with_circuit() -> object:
                    response: object = await circuit.call(_do)
                    content = (getattr(response, "content", "") or "").strip()
                    reasoning = (getattr(response, "reasoning", "") or "").strip()
                    if not content and not reasoning:
                        extract_content(response, request_id)
                    return response

                response: object = await with_llm_retry(_do_with_circuit, timeout, 0)
                content = (getattr(response, "content", "") or "").strip()
                reasoning = (getattr(response, "reasoning", "") or "").strip()
                reasoning_only = not content
                head = content[:16] or reasoning[:16]
                record_usage(metrics, app_name, response, time.monotonic() - start, None)
                await _charge("chat", app_name, request_id, model=str(getattr(response, "model", "") or ""))
                return {
                    "ok": True,
                    "reply_head": head,
                    "reasoning_only": reasoning_only,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "model": str(getattr(response, "model", "") or ""),
                    "error": "",
                }
        except Exception as exc:
            return {
                "ok": False,
                "reply_head": "",
                "reasoning_only": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "model": "",
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }

    async def extract(
        self,
        prompt: str,
        schema: Optional[type] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        raise_on_error: bool = False,
        feature: str = "",
        task_type: Optional[str] = None,
    ) -> Optional[object]:
        await configure_ironman()
        from ironman import extract as _extract
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        circuit = get_llm_circuit()
        await _preflight("extract")

        async def _do() -> object:
            with credits_gateway_handled():
                return await _extract(
                    prompt=prompt,
                    schema=schema,
                    llm=LLMOptions(extra={"task_type": task_type} if task_type else None),
                )

        try:
            async with llm_telemetry("extract", app_name, request_id) as (metrics, start):
                async def _do_with_circuit() -> object:
                    return await circuit.call(_do)
                result: object = await with_llm_retry(
                    _do_with_circuit, timeout, effective_retries(max_retries)
                )
                metrics.record(app_name, "unknown", time.monotonic() - start, tokens=0, error=None)
                await _meter("extract", app_name, request_id, start=start, feature=feature)
                await _charge("extract", app_name, request_id)
                logger.info(
                    "LLM extract completed [req_id=%s, app=%s, latency=%.2fs]",
                    request_id, app_name, time.monotonic() - start,
                )
                return result
        except Exception:
            if raise_on_error:
                raise
            return None

    async def embed(
        self,
        texts: list[str],
        timeout: float = 60.0,
        max_retries: int = 3,
        raise_on_error: bool = False,
        feature: str = "",
    ) -> Optional[list[list[float]]]:
        await configure_ironman()
        try:
            from ironman.tracing.request_scope import set_outbound_request_id

            from nexus.trace import ensure_request_id

            set_outbound_request_id(ensure_request_id())
        except ImportError:
            pass
        from ironman import embed as _embed

        app_name: str = resolve_app_name()
        request_id: str = get_request_id() or "-"
        await _preflight("embed")

        async def _do() -> list[list[float]]:
            with credits_gateway_handled():
                return await _embed(text=texts)

        try:
            result = await with_llm_retry(_do, timeout, effective_retries(max_retries))
            await _meter("embed", app_name, request_id, calls=len(texts) or 1, feature=feature)
            await _charge("embed", app_name, request_id)
            return result
        except Exception as e:
            logger.error("Embed failed: %s: %s", type(e).__name__, e or "(无错误详情)")
            if raise_on_error:
                raise
            return None
