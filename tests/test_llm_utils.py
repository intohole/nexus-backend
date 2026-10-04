"""llm_utils JSON 解析家族与重试判定的回归测试（1.26.0 补齐测试洞）。"""
from __future__ import annotations

import asyncio

import pytest

from nexus.llm_utils import (
    LLMTimeoutError,
    find_balanced_json,
    is_retryable_error,
    parse_llm_json,
    parse_llm_json_lenient,
    parse_llm_json_or,
    strip_code_fence,
    with_retry,
)


class TestStripCodeFence:
    def test_plain_passthrough(self):
        assert strip_code_fence('{"a": 1}') == '{"a": 1}'

    def test_fenced_with_language(self):
        assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_fenced_without_closing(self):
        assert strip_code_fence('```\n{"a": 1}') == '{"a": 1}'

    def test_fenced_multiline_keeps_inner_lines(self):
        text = strip_code_fence('```json\n{"a": 1,\n "b": 2}\n```')
        assert text == '{"a": 1,\n "b": 2}'


class TestFindBalancedJson:
    def test_nested_braces(self):
        text = '前置 {"a": {"b": [1, 2]}} 后置'
        assert find_balanced_json(text) == '{"a": {"b": [1, 2]}}'

    def test_invalid_candidate_skipped(self):
        text = '{"bad": json} {"good": 1}'
        assert find_balanced_json(text) == '{"good": 1}'

    def test_no_json_returns_none(self):
        assert find_balanced_json('没有任何 JSON') is None

    def test_braces_inside_string_known_limitation(self):
        """已知边界：括号计数不感知字符串字面量，字符串内 }
        会提前闭合候选；parse_llm_json 的 regex 兜底会接住此类输入。"""
        text = '{"s": "包含 } 的字符串"}'
        assert find_balanced_json(text) is None


class TestParseLlmJson:
    def test_direct_dict(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_fenced_dict(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_dict_in_prose(self):
        assert parse_llm_json('结论如下：{"company": "腾讯", "pos": "后端"} 以上。') == {"company": "腾讯", "pos": "后端"}

    def test_trailing_comma_repair(self):
        assert parse_llm_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_unquoted_keys_repair(self):
        assert parse_llm_json('{company: "腾讯"}') == {"company": "腾讯"}

    def test_non_dict_top_level_returns_sentinel(self):
        result = parse_llm_json('[1, 2, 3]')
        assert result == {"raw_response": "[1, 2, 3]"}

    def test_total_failure_returns_sentinel(self):
        result = parse_llm_json('完全不是 JSON')
        assert set(result.keys()) == {"raw_response"}

    def test_sentinel_contract_documented(self):
        """失败兜底是 {"raw_response": ...} 而非抛错——调用方需自查哨兵。"""
        result = parse_llm_json('```json\n{broken\n```')
        assert "raw_response" in result

    def test_fallback_none_returns_none_on_failure(self):
        assert parse_llm_json('完全不是 JSON', fallback=None) is None

    def test_fallback_dict_returns_fallback_on_failure(self):
        assert parse_llm_json('```json\n{broken\n```', fallback={"items": []}) == {"items": []}

    def test_fallback_not_used_on_success(self):
        assert parse_llm_json('{"a": 1}', fallback=None) == {"a": 1}

    def test_fallback_preserves_repair_chain(self):
        assert parse_llm_json('{"a": 1,}', fallback=[]) == {"a": 1}


class TestParseLlmJsonLenient:
    def test_object(self):
        assert parse_llm_json_lenient('{"a": 1}') == {"a": 1}

    def test_top_level_array(self):
        assert parse_llm_json_lenient('[{"id": 1}, {"id": 2}]') == [{"id": 1}, {"id": 2}]

    def test_fenced_array(self):
        assert parse_llm_json_lenient('```json\n["x", "y"]\n```') == ["x", "y"]

    def test_array_field_object_not_mistaken(self):
        """对象含数组字段时不得被错提为内层数组（lenient 正则数组优先的边界）。"""
        raw = '{"items": [1, 2], "total": 2}'
        assert parse_llm_json_lenient(raw) == {"items": [1, 2], "total": 2}

    def test_failure_returns_none(self):
        assert parse_llm_json_lenient('无 JSON') is None


class TestParseLlmJsonOr:
    def test_success_passthrough(self):
        assert parse_llm_json_or('{"a": 1}', {}) == {"a": 1}

    def test_failure_returns_default(self):
        sentinel = ["default"]
        assert parse_llm_json_or('垃圾输出', sentinel) is sentinel

    def test_json_null_returns_default(self):
        sentinel = {"fallback": True}
        assert parse_llm_json_or('null', sentinel) is sentinel


class TestIsRetryableError:
    def test_client_error_not_retryable(self):
        assert is_retryable_error(RuntimeError("Client error '401 Unauthorized'")) is False
        assert is_retryable_error(RuntimeError("status_code=400 bad request")) is False

    def test_rate_limit_and_server_error_retryable(self):
        assert is_retryable_error(RuntimeError("status_code=429")) is True
        assert is_retryable_error(RuntimeError("returned 503 service unavailable")) is True

    def test_timeout_retryable(self):
        assert is_retryable_error(asyncio.TimeoutError()) is True
        assert is_retryable_error(LLMTimeoutError("timeout")) is True

    def test_circuit_open_not_retryable(self):
        class CircuitBreakerOpenError(Exception):
            pass

        assert is_retryable_error(CircuitBreakerOpenError("open")) is False

    def test_unknown_conservatively_retryable(self):
        assert is_retryable_error(RuntimeError("weird failure")) is True


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        async def coro_fn():
            return 42

        assert await with_retry(coro_fn, max_retries=1) == 42

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self, monkeypatch):
        sleeps: list[float] = []

        async def fast_sleep(_: float) -> None:
            sleeps.append(_)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        class BusinessError(Exception):
            pass

        calls = 0

        async def coro_fn():
            nonlocal calls
            calls += 1
            raise BusinessError("no retry")

        with pytest.raises(BusinessError):
            await with_retry(coro_fn, max_retries=3, non_retryable=(BusinessError,))
        assert calls == 1
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self, monkeypatch):
        async def fast_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        calls = 0

        async def coro_fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("transient")
            return "ok"

        assert await with_retry(coro_fn, max_retries=3) == "ok"
        assert calls == 3
