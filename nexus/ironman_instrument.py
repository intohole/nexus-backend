"""ironman 插桩：包装 chat/embed/ask/extract/chat_stream 注入 metrics/circuit/req_id。

所有业务应用直接调用 ironman 模块级函数，不经过 nexus.llm.LLMService 的路径
（如 goldenStock 直调 ironman.chat_stream），因此在 init_ironman() 完成后
包装 ironman 模块级函数，确保插桩对所有调用方生效。
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from nexus.logging import get_logger

logger = get_logger("nexus.ironman.instr")

# A4: 插桩状态（避免重复包装）
_instrumented: bool = False


def _embed_provider_key(provider: object) -> str:
    """embed 熔断通道键单源（r267 刀3）：显式 provider 直取，空则默认通道（同键空间 llm:）。"""
    from nexus.circuit_breaker import llm_provider_key

    return str(provider or "") or llm_provider_key(None)


def _classify_failure(exc: BaseException) -> str:
    """r270 刀8②：五面失败分类单源——provider 失败才进熔断通道，取消/中止/本地
    异常不再连坐默认通道（旧形态 except Exception 全记=本地 pydantic/ConfigError
    也喂熔断、g321 同族取消超时一次不计，与「宁可多熔不可漏熔」声明相反）。

    返回："provider"（记熔断）| "cancelled"（re-raise 不记）| "aborted"（不记）|
    "local"（记 metrics 不记熔断）。"""
    import asyncio

    from ironman.errors import ConfigError
    from ironman.middleware.pipeline import AbortedError

    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, AbortedError):
        return "aborted"
    if isinstance(exc, (ValueError, TypeError, KeyError, ConfigError)):
        return "local"
    try:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            return "local"
    except ImportError:  # pragma: no cover
        pass
    return "provider"


def _instrument_ironman(app_name: str) -> None:
    """包装 ironman 模块级函数，注入 metrics/circuit_breaker/req_id。幂等。"""
    global _instrumented
    if _instrumented:
        return

    import ironman as _ironman_mod

    original_chat = _ironman_mod.chat
    original_embed = _ironman_mod.embed
    original_ask = getattr(_ironman_mod, "ask", None)
    original_extract = getattr(_ironman_mod, "extract", None)
    original_chat_stream = getattr(_ironman_mod, "chat_stream", None)

    def _ctx() -> tuple:
        from nexus.context import get_request_id
        from nexus.llm_metrics import get_llm_metrics
        return (
            get_request_id() or "-",
            app_name or "unknown",
            get_llm_metrics(),
            time.monotonic(),
        )

    def _entry_circuit(llm: object) -> object:
        from nexus.circuit_breaker import get_llm_circuit, llm_provider_key

        return get_llm_circuit(llm_provider_key(llm))

    def _entry_circuit_embed(provider: object) -> object:
        from nexus.circuit_breaker import get_llm_circuit

        return get_llm_circuit(_embed_provider_key(provider))

    async def _record_embed(provider: object, error: object, response: object = None) -> None:
        from nexus.circuit_breaker import record_llm_outcome
        await record_llm_outcome(_embed_provider_key(provider), error, response)

    def _usage(result: object) -> tuple:
        tokens: int = 0
        cached: int = 0
        usage = getattr(result, "usage", None)
        if usage:
            tokens = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
            cached = int(getattr(usage, "cached_tokens", 0) or 0)
        model: str = getattr(result, "model", None) or "unknown"
        return model, tokens, cached

    def _ok(op: str, req_id: str, app: str, metrics: object, model: str, start: float, tokens: int = 0, cached_tokens: int = 0) -> float:
        latency: float = time.monotonic() - start
        metrics.record(app, model, latency, tokens=tokens, error=None, cached_tokens=cached_tokens)
        logger.info("LLM %s [req_id=%s, app=%s, model=%s, latency=%.2fs, tokens=%d]",
                    op, req_id, app, model, latency, tokens)
        return latency

    def _fail(op: str, req_id: str, app: str, metrics: object, start: float, e: Exception, model: str = "unknown") -> None:
        latency: float = time.monotonic() - start
        error_type: str = type(e).__name__
        metrics.record(app, model, latency, tokens=0, error=error_type)
        log_fn = logger.warning if error_type == "CircuitBreakerOpenError" else logger.error
        log_fn("LLM %s failed [req_id=%s, app=%s, model=%s, latency=%.2fs]: %s: %s",
               op, req_id, app, model, latency, error_type, e or "(无错误详情)")

    async def _wrapped_chat(messages: object, llm: object = None, tools: object = None) -> object:
        request_id, app, metrics, start = _ctx()
        circuit = _entry_circuit(llm)

        async def _do() -> object:
            return await original_chat(messages, llm=llm, tools=tools)  # type: ignore[misc]

        try:
            await circuit.acquire_call()
            result: object = await _do()
            await _record(llm, None, result)
            model, tokens, cached = _usage(result)
            _ok("chat", request_id, app, metrics, model, start, tokens, cached_tokens=cached)
            return result
        except Exception as e:
            # r270 刀8②：失败分类（provider 才进熔断；cancelled/aborted/local 不连坐）
            if _classify_failure(e) == "provider":
                await _record(llm, e)
            _fail("chat", request_id, app, metrics, start, e)
            raise
        except asyncio.CancelledError:
            metrics.record(app, "chat", time.monotonic() - start, tokens=0, error="cancelled")
            raise

    async def _wrapped_embed(
        text: object, model: object = None, provider: object = None,
        dimensions: object = None, encoding_format: object = None,
    ) -> object:
        request_id, app, metrics, start = _ctx()
        circuit = _entry_circuit_embed(provider)
        emb_model: str = f"embed:{model or 'unknown'}"
        try:
            await circuit.acquire_call()
            result: object = await original_embed(  # type: ignore[misc]
                text, model=model, provider=provider,
                dimensions=dimensions, encoding_format=encoding_format,
            )
            await _record_embed(provider, None, result)
            _ok("embed", request_id, app, metrics, emb_model, start)
            return result
        except Exception as e:
            # r270 刀8②：embed 同款失败分类（第五面旁路修复的诚实性配套）
            if _classify_failure(e) == "provider":
                await _record_embed(provider, e)
            _fail("embed", request_id, app, metrics, start, e, model=emb_model)
            raise

    async def _record(llm: object, error: object, response: object = None) -> None:
        from nexus.circuit_breaker import llm_provider_key, record_llm_outcome
        await record_llm_outcome(llm_provider_key(llm), error, response)

    async def _circuit_op(op: str, original: Callable[..., Awaitable[object]],
                          *args: object, **kwargs: object) -> object:
        request_id, app, metrics, start = _ctx()
        llm = kwargs.get("llm")
        circuit = _entry_circuit(llm)

        async def _do() -> object:
            return await original(*args, **kwargs)  # type: ignore[misc]

        try:
            await circuit.acquire_call()
            result: object = await _do()
            await _record(llm, None, result)
            model, tokens, cached = _usage(result)
            _ok(op, request_id, app, metrics, model, start, tokens, cached_tokens=cached)
            return result
        except Exception as e:
            # r270 刀8②：失败分类（ask/extract 共用 _circuit_op 通道）
            if _classify_failure(e) == "provider":
                await _record(llm, e)
            _fail(op, request_id, app, metrics, start, e)
            raise
        except asyncio.CancelledError:
            metrics.record(app, str(op), time.monotonic() - start, tokens=0, error="cancelled")
            raise

    async def _wrapped_ask(*args: object, **kwargs: object) -> object:
        return await _circuit_op("ask", original_ask, *args, **kwargs)  # type: ignore[misc]

    async def _wrapped_extract(*args: object, **kwargs: object) -> object:
        schema_obj = kwargs.get("schema") if kwargs else None
        schema_name: str = (getattr(schema_obj, "__name__", None) or "extract") if schema_obj is not None else "raw"
        request_id, app, metrics, start = _ctx()
        llm = kwargs.get("llm")
        circuit = _entry_circuit(llm)
        try:
            await circuit.acquire_call()
            result: object = await original_extract(*args, **kwargs)  # type: ignore[misc]
            await _record(llm, None, result)
            _ok("extract", request_id, app, metrics, f"extract:{schema_name}", start)
            return result
        except Exception as e:
            await _record(llm, e)
            _fail("extract", request_id, app, metrics, start, e, model="extract:unknown")
            raise

    def _wrapped_stream(*args: object, **kwargs: object) -> object:
        async def _gen() -> object:
            from nexus.circuit_breaker import CircuitBreakerOpenError, llm_provider_key

            request_id, app, metrics, start = _ctx()
            llm = kwargs.get("llm")
            circuit = _entry_circuit(llm)

            try:
                await circuit.acquire_call()
            except CircuitBreakerOpenError:
                metrics.record(app, "stream", 0.0, tokens=0, error="CircuitBreakerOpenError")
                logger.warning(
                    "LLM stream blocked by open circuit [req_id=%s, app=%s, provider=%s]",
                    request_id, app, llm_provider_key(llm),
                )
                raise

            try:
                # r271 刀1c：采集 chunk.model/usage（病行修：旧 model 恒 stream/tokens 恒 0）
                last_model, last_usage = "stream", None
                async for chunk in original_chat_stream(*args, **kwargs):  # type: ignore[misc]
                    last_model = str(getattr(chunk, "model", "") or "") or last_model
                    last_usage = chunk.usage if getattr(chunk, "usage", None) is not None else last_usage
                    yield chunk
                await _record(llm, None, None)
                u = last_usage
                total = int((getattr(u, "prompt_tokens", 0) or 0) + (getattr(u, "completion_tokens", 0) or 0)) if u else 0
                cached = int(getattr(u, "cached_tokens", 0) or 0) if u else 0
                _ok("stream", request_id, app, metrics, last_model, start, total, cached_tokens=cached)
            except Exception as e:
                if _classify_failure(e) == "provider":
                    await _record(llm, e)
                _fail("stream", request_id, app, metrics, start, e, model="stream")
                raise
            except asyncio.CancelledError:
                metrics.record(app, "stream", time.monotonic() - start, tokens=0, error="cancelled")
                raise

        return _gen()

    _ironman_mod.chat = _wrapped_chat
    _ironman_mod.embed = _wrapped_embed
    if original_ask is not None:
        _ironman_mod.ask = _wrapped_ask
    if original_extract is not None:
        _ironman_mod.extract = _wrapped_extract
    if original_chat_stream is not None:
        _ironman_mod.chat_stream = _wrapped_stream
    _instrument_facade_credits(app_name)
    _instrumented = True
    logger.info("ironman instrumented (chat + embed + ask + extract + chat_stream wrapped with metrics/circuit/req_id; facade _execute/_stream_engine wrapped with credits metering/charging)")


def _detect_vision_request(request: object) -> bool:
    """请求含图（模型名带 vision 或消息含图片分片）→ kind=vision。"""
    if "vision" in str(getattr(request, "model", "") or "").lower():
        return True
    try:
        for msg in (getattr(request, "messages", None) or []):
            for part in (getattr(msg, "content", None) or [] if isinstance(getattr(msg, "content", None), list) else []):
                if str(getattr(part, "type", "") or "") in ("image_url", "image") or hasattr(part, "image_url"):
                    return True
    except Exception:
        pass
    return False


def _instrument_facade_credits(app_name: str) -> None:
    """ironman facade 层计量+计费（绕过 LLMService 的调用统一归账）。

    _execute=非流式唯一执行点（三命名空间同名引用都换绑，覆盖函数内 from-import）；
    _stream_engine=流式唯一引擎；LLMService 出口经 credits_gateway_handled() 防双计；
    全程 fail-open：计量/计费/预检失败绝不影响调用本身。
    """
    import ironman._facade as facade_pkg
    import ironman._facade_core as facade_core
    import ironman._facade_stream as facade_stream
    import ironman._facade_vision as facade_vision

    original_execute = facade_core._execute
    original_stream_engine = facade_stream._stream_engine

    async def _safe_preflight(kind: str, model: str) -> None:
        try:
            from nexus.credits import CreditsInsufficientError, get_credits_service

            await get_credits_service().gateway_preflight(kind, model=model)
        except Exception as exc:
            from nexus.credits import CreditsInsufficientError

            if isinstance(exc, CreditsInsufficientError):
                raise
            logger.debug("facade 计费预检失败(放行): %s", exc)

    def _usage_from_usage(usage: object) -> dict | None:
        if usage is None:
            return None
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        if input_tokens <= 0 and output_tokens <= 0:
            return None
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
            "called_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }

    async def _meter_charge(
        kind: str, request_id: str, model: str, response: object, latency_s: float,
        usage: dict | None = None,
    ) -> None:
        try:
            from nexus.credits import get_credits_service, report_llm_usage

            await report_llm_usage(
                app_name=app_name, kind=kind, request_id=request_id,
                response=response, latency_s=latency_s,
            )
            service = get_credits_service()
            if service.gateway_feature(kind):
                await service.auto_charge_llm(
                    kind=kind, app_name=app_name, request_id=request_id, model=model,
                    usage=usage if usage is not None else _usage_from_usage(getattr(response, "usage", None)),
                )
        except Exception as exc:
            logger.debug("facade 计量计费失败(忽略): %s", exc)

    async def _credits_execute(request: object, provider_name: object = None) -> object:
        from nexus.credits import gateway_handled

        if gateway_handled():
            return await original_execute(request, provider_name)  # type: ignore[misc]
        from nexus.context import get_request_id

        kind = "vision" if _detect_vision_request(request) else "chat"
        model = str(getattr(request, "model", "") or "")
        await _safe_preflight(kind, model)
        start = time.monotonic()
        result = await original_execute(request, provider_name)  # type: ignore[misc]
        await _meter_charge(
            kind, get_request_id() or "-", str(getattr(result, "model", "") or model),
            result, time.monotonic() - start,
        )
        return result

    def _credits_stream_engine(request: object, llm_opts: object, kind: str = "chat_stream",
                               *args: object, **kwargs: object) -> object:
        async def _gen() -> object:
            from nexus.context import get_request_id
            from nexus.credits import gateway_handled

            if gateway_handled():
                async for chunk in original_stream_engine(request, llm_opts, kind, *args, **kwargs):  # type: ignore[misc]
                    yield chunk
                return
            feature_kind = "vision" if kind == "stream_vision" else "chat"
            model = str(getattr(request, "model", "") or "")
            await _safe_preflight(feature_kind, model)
            start = time.monotonic()
            produced = False
            last_usage = None
            try:
                async for chunk in original_stream_engine(request, llm_opts, kind, *args, **kwargs):  # type: ignore[misc]
                    if getattr(chunk, "content", ""):
                        produced = True
                    if getattr(chunk, "usage", None) is not None:
                        last_usage = chunk.usage
                    yield chunk
            finally:
                if produced:
                    if last_usage is None:
                        # r270 刀8③：断连零账显影（已产字节白交付、usage 未达，不发明计费）
                        logger.warning("stream 断连零账（已产字节无 usage chunk，按 0 计费）: kind=%s", feature_kind)
                    await _meter_charge(feature_kind, get_request_id() or "-", model, None,
                                        time.monotonic() - start, usage=_usage_from_usage(last_usage))

        return _gen()

    facade_core._execute = _credits_execute  # type: ignore[assignment]
    facade_pkg._execute = _credits_execute  # type: ignore[assignment]
    facade_vision._execute = _credits_execute  # type: ignore[assignment]
    facade_stream._stream_engine = _credits_stream_engine  # type: ignore[assignment]
    logger.info("ironman facade credits instrumented (_execute ×3 + _stream_engine wrapped with metering/charging)")
