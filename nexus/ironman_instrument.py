"""ironman 插桩：包装 chat/embed/ask/extract/chat_stream 注入 metrics/circuit/req_id。

所有业务应用直接调用 ironman 模块级函数，不经过 nexus.llm.LLMService 的路径
（如 goldenStock 直调 ironman.chat_stream），因此在 init_ironman() 完成后
包装 ironman 模块级函数，确保插桩对所有调用方生效。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from nexus.logging import get_logger

logger = get_logger("nexus.ironman.instr")

# A4: 插桩状态（避免重复包装）
_instrumented: bool = False


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
        from nexus.circuit_breaker import get_llm_circuit
        return (
            get_request_id() or "-",
            app_name or "unknown",
            get_llm_circuit(),
            get_llm_metrics(),
            time.monotonic(),
        )

    def _usage(result: object) -> tuple:
        tokens: int = 0
        usage = getattr(result, "usage", None)
        if usage:
            tokens = (getattr(usage, "prompt_tokens", 0) or 0) + (
                getattr(usage, "completion_tokens", 0) or 0
            )
        model: str = getattr(result, "model", None) or "unknown"
        return model, tokens

    def _ok(op: str, req_id: str, app: str, metrics: object, model: str, start: float, tokens: int = 0) -> float:
        latency: float = time.monotonic() - start
        metrics.record(app, model, latency, tokens=tokens, error=None)
        logger.info(
            "LLM %s [req_id=%s, app=%s, model=%s, latency=%.2fs, tokens=%d]",
            op, req_id, app, model, latency, tokens,
        )
        return latency

    def _fail(op: str, req_id: str, app: str, metrics: object, start: float, e: Exception, model: str = "unknown") -> None:
        latency: float = time.monotonic() - start
        error_type: str = type(e).__name__
        metrics.record(app, model, latency, tokens=0, error=error_type)
        log_fn = logger.warning if error_type == "CircuitBreakerOpenError" else logger.error
        log_fn(
            "LLM %s failed [req_id=%s, app=%s, latency=%.2fs]: %s: %s",
            op, req_id, app, latency, error_type, e or "(无错误详情)",
        )

    async def _wrapped_chat(messages: object, llm: object = None, tools: object = None) -> object:
        request_id, app, circuit, metrics, start = _ctx()

        async def _do() -> object:
            return await original_chat(messages, llm=llm, tools=tools)  # type: ignore[misc]

        try:
            result: object = await circuit.call(_do)
            model, tokens = _usage(result)
            _ok("chat", request_id, app, metrics, model, start, tokens)
            return result
        except Exception as e:
            _fail("chat", request_id, app, metrics, start, e)
            raise

    async def _wrapped_embed(
        text: object, model: object = None, provider: object = None,
        dimensions: object = None, encoding_format: object = None,
    ) -> object:
        request_id, app, _circuit, metrics, start = _ctx()
        try:
            result: object = await original_embed(  # type: ignore[misc]
                text, model=model, provider=provider,
                dimensions=dimensions, encoding_format=encoding_format,
            )
            emb_model: str = model or "unknown"
            _ok("embed", request_id, app, metrics, f"embed:{emb_model}", start)
            return result
        except Exception as e:
            _fail("embed", request_id, app, metrics, start, e, model="embed:unknown")
            raise

    async def _circuit_op(op: str, original: Callable[..., Awaitable[object]],
                          *args: object, **kwargs: object) -> object:
        request_id, app, circuit, metrics, start = _ctx()

        async def _do() -> object:
            return await original(*args, **kwargs)  # type: ignore[misc]

        try:
            result: object = await circuit.call(_do)
            model, tokens = _usage(result)
            _ok(op, request_id, app, metrics, model, start, tokens)
            return result
        except Exception as e:
            _fail(op, request_id, app, metrics, start, e)
            raise

    async def _wrapped_ask(*args: object, **kwargs: object) -> object:
        return await _circuit_op("ask", original_ask, *args, **kwargs)  # type: ignore[misc]

    async def _wrapped_extract(*args: object, **kwargs: object) -> object:
        schema_obj = kwargs.get("schema") if kwargs else None
        schema_name: str = "raw"
        if schema_obj is not None:
            schema_name = getattr(schema_obj, "__name__", None) or "extract"
        request_id, app, circuit, metrics, start = _ctx()

        async def _do() -> object:
            return await original_extract(*args, **kwargs)  # type: ignore[misc]

        try:
            result: object = await circuit.call(_do)
            _ok("extract", request_id, app, metrics, f"extract:{schema_name}", start)
            return result
        except Exception as e:
            _fail("extract", request_id, app, metrics, start, e, model="extract:unknown")
            raise

    def _wrapped_stream(*args: object, **kwargs: object) -> object:
        async def _gen() -> object:
            from nexus.circuit_breaker import CircuitBreakerOpenError, CircuitState

            request_id, app, circuit, metrics, start = _ctx()

            if circuit.state == CircuitState.OPEN:
                metrics.record(app, "stream", 0.0, tokens=0, error="CircuitBreakerOpenError")
                logger.warning(
                    "LLM stream blocked by open circuit [req_id=%s, app=%s]",
                    request_id, app,
                )
                raise CircuitBreakerOpenError("Circuit 'llm_gateway' is OPEN")

            try:
                async for chunk in original_chat_stream(*args, **kwargs):  # type: ignore[misc]
                    yield chunk
                _ok("stream", request_id, app, metrics, "stream", start)
            except Exception as e:
                _fail("stream", request_id, app, metrics, start, e, model="stream")
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
    model = str(getattr(request, "model", "") or "").lower()
    if "vision" in model:
        return True
    try:
        for msg in (getattr(request, "messages", None) or []):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    ptype = str(getattr(part, "type", "") or "")
                    if ptype in ("image_url", "image") or hasattr(part, "image_url"):
                        return True
    except Exception:
        pass
    return False


def _instrument_facade_credits(app_name: str) -> None:
    """ironman facade 层计量+计费：绕过 LLMService 的调用在此统一归账。

    - _execute 是全部非流式调用的唯一执行点（模块 chat/ask/extract、_execute_vision、
      AgentBuilder、技能引擎均汇聚于此）；_facade_core/_facade/_facade_vision 三个
      命名空间的同名引用都换绑，覆盖函数内 from-import 的绑定。
    - _stream_engine 是全部流式调用引擎（chat_stream/stream_vision 共用，第三参即 kind）。
    - LLMService 出口已计费的调用经 credits_gateway_handled() 标记跳过，防双计。
    - 全程 fail-open：计量/计费/预检失败绝不影响调用本身。
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

    async def _meter_charge(
        kind: str, request_id: str, model: str, response: object, latency_s: float
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
                    kind=kind, app_name=app_name, request_id=request_id, model=model
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
            from nexus.credits import gateway_handled
            from nexus.context import get_request_id

            if gateway_handled():
                async for chunk in original_stream_engine(request, llm_opts, kind, *args, **kwargs):  # type: ignore[misc]
                    yield chunk
                return
            feature_kind = "vision" if kind == "stream_vision" else "chat"
            model = str(getattr(request, "model", "") or "")
            await _safe_preflight(feature_kind, model)
            start = time.monotonic()
            produced = False
            try:
                async for chunk in original_stream_engine(request, llm_opts, kind, *args, **kwargs):  # type: ignore[misc]
                    if getattr(chunk, "content", ""):
                        produced = True
                    yield chunk
            finally:
                if produced:
                    await _meter_charge(
                        feature_kind, get_request_id() or "-", model, None,
                        time.monotonic() - start,
                    )

        return _gen()

    facade_core._execute = _credits_execute  # type: ignore[assignment]
    facade_pkg._execute = _credits_execute  # type: ignore[assignment]
    facade_vision._execute = _credits_execute  # type: ignore[assignment]
    facade_stream._stream_engine = _credits_stream_engine  # type: ignore[assignment]
    logger.info("ironman facade credits instrumented (_execute ×3 + _stream_engine wrapped with metering/charging)")
