"""LLM 通用工具：超时/可重试判定、重试等待解析与元叙述判定。

JSON 提取解析族已拆至 nexus.llm_json（本文件超限拆分）；同名符号在此
re-export 保持新旧导入路径等价（业务仓直引 nexus.llm_utils 不受影响）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, TypeVar

from nexus.llm_json import (  # noqa: F401
    find_balanced_json as find_balanced_json,
    is_llm_json_error as is_llm_json_error,
    parse_json_column as parse_json_column,
    parse_llm_json as parse_llm_json,
    parse_llm_json_lenient as parse_llm_json_lenient,
    parse_llm_json_or as parse_llm_json_or,
    strip_code_fence as strip_code_fence,
)

logger = logging.getLogger("nexus.llm_utils")

T = TypeVar("T")

# P1: 不可重试的 HTTP 状态码（4xx 客户端错误，除 429 限流）
_NON_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("400", "401", "403", "404", "422")
# P1: 可重试的 HTTP 状态码（429 限流、5xx 服务端错误）
_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("429", "500", "502", "503", "504")


class LLMTimeoutError(Exception):
    """LLM 调用超时异常。"""
    pass


class LLMEmptyContentError(RuntimeError):
    """LLM 返回空 content（含 reasoning-only 输出）。

    reasoning 是思考过程而非业务内容，禁止兜底充当 content（2026-10-22
    verseCraft novel 8 实锤：思考独白整章落库）。空 content 属瞬时故障
    （thinking 吃光预算，S27 实测约 50% 概率），交由 with_llm_retry 重试。
    """
    pass


def is_retryable_error(exc: Exception) -> bool:
    """P1: 判断异常是否值得重试。

    策略：
    - 熔断器 OPEN：不可重试（重试只会被熔断器再次拒绝）
    - 超时/连接错误：可重试（瞬时故障）
    - 4xx 客户端错误（除 429）：不可重试（请求本身有问题，重试无用）
    - 429 限流、5xx 服务端错误：可重试
    - 未知错误：保守重试（宁可多重试也不漏重试）
    """
    # A4.2: 熔断器 OPEN 状态：不可重试（用字符串匹配避免循环导入）
    exc_type_name: str = type(exc).__name__
    if exc_type_name == "CircuitBreakerOpenError":
        return False

    # 超时错误：可重试
    if isinstance(exc, (asyncio.TimeoutError, LLMTimeoutError)):
        return True
    # 连接错误：可重试
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    # httpx 连接/超时错误：可重试
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.PoolTimeout)):
            return True
        # httpx HTTPStatusError：直接读取 status_code 属性，避免字符串匹配遗漏
        if isinstance(exc, httpx.HTTPStatusError):
            code = str(exc.response.status_code)
            if code in _NON_RETRYABLE_STATUS_CODES:
                return False
            if code in _RETRYABLE_STATUS_CODES:
                return True
    except ImportError:
        pass

    msg = str(exc).lower()
    # 不可重试：4xx 客户端错误（除 429），匹配多种格式：
    #   "status_code=401" / "returned 401" / " 401:" / "'401 unauthorized'" / "error 401"
    code = _extract_status_code(msg)
    if code:
        if code in _NON_RETRYABLE_STATUS_CODES:
            return False
        if code in _RETRYABLE_STATUS_CODES:
            return True
    # 默认：未知错误重试（保守策略）
    return True


_STATUS_CODE_RE = re.compile(
    r"(?:status_code[=\s]+|returned\s+|error\s+['\"]?|http\s+|['\"])(\d{3})\b",
    re.IGNORECASE,
)


def _extract_status_code(msg: str) -> str | None:
    m = _STATUS_CODE_RE.search(msg)
    return m.group(1) if m else None


def _extract_retry_after(exc: Exception) -> float | None:
    """P3: 从异常消息中提取 retry_after（秒）。

    匹配 "retry_after": 5、retry_after=5、Retry-After: 5 等格式。
    返回 None 表示未找到。
    """
    msg = str(exc)
    patterns = [
        r'"retry_after"[:\s]+(\d+(?:\.\d+)?)',
        r"retry_after[=\s:]+(\d+(?:\.\d+)?)",
        r"Retry-After[=\s:]+(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, msg, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                pass
    return None


_META_START_RE = None


def is_meta_narration(text: str) -> dict:
    """判断 LLM 输出是否为「分析过程/工作日志」而非可直接发布的正文。

    verseCraft r15 生产实锤：作者把修改指令贴进生成入口，推理模型输出
    整段修改分析独白（「用户希望我…让我逐一分析」）被当正文落库发布。
    凡「LLM 输出 → 落库/发布」链路（章节正文/简历/讲义）都应在落库前
    单源调用本函数，而不是各自攒正则。

    判定纪律：宁缺毋滥——正文（含对话、编号引文）不得误杀；
    全信号确定性零 LLM，同一输入重复调用同结果。

    Returns:
        {"is_meta": bool, "signals": list[str], "process_hits": int,
         "numbered_lines": int, "dialogue_marks": int}
    """
    import re as _re

    global _META_START_RE
    if _META_START_RE is None:
        _META_START_RE = _re.compile(
            r"^(?:用户希望|让我|我来|我需要|我选择|好的[，,、]|明白了|收到[。！]|"
            r"首先[，,]?.{0,8}(?:分析|看|处理)|根据.{0,16}(?:指令|要求|意见)|"
            r"以下是我|这是修改|修改说明|关于.{0,12}修改)"
        )
    process_patterns = [
        r"让我(?:想想|重新想|分析|看|梳理)", r"我(?:选择|倾向|认为应该|决定)",
        r"(?:逐一|逐条|依次)(?:分析|检查|处理|执行)", r"(?:分析|思考)如下",
        r"按照(?:用户|修改|他的|她的)?指令", r"(?:指令|第)\s*\d+\s*[:：]",
        r"(?:选项|方案)(?:是|有)[:：]?", r"如果(?:我)?选择", r"两个版本",
        r"更(?:合理|戏剧化|简单)", r"(?:需要|需要相应)修改", r"原文(?:说|中)",
    ]
    numbered_re = _re.compile(
        r"(?:^|[：:。！？\n])\s*\d+\s*[\.、．]\s*(?:将|把|在|修改|改为|删|补|保持|若|指令|第)",
        _re.M,
    )
    dialogue_re = _re.compile(r"[「『“\"]")

    verdict = {"is_meta": False, "signals": [], "process_hits": 0,
               "numbered_lines": 0, "dialogue_marks": 0}
    if not text or not text.strip():
        return verdict
    head = text.strip()[:120]
    sample = text[:2500]

    verdict["dialogue_marks"] = len(dialogue_re.findall(sample))
    verdict["numbered_lines"] = len(numbered_re.findall(sample))
    verdict["process_hits"] = sum(1 for p in process_patterns if _re.search(p, sample))

    if _META_START_RE.search(head):
        verdict["signals"].append("开头即工作日志口吻")
        verdict["is_meta"] = True
    if verdict["numbered_lines"] >= 3 and verdict["process_hits"] >= 3:
        verdict["signals"].append(f"编号指令复述 {verdict['numbered_lines']} 行")
        verdict["is_meta"] = True
    if not verdict["is_meta"] and verdict["process_hits"] >= 5 and verdict["dialogue_marks"] <= 2:
        verdict["signals"].append(f"过程性话术 {verdict['process_hits']} 处且几乎无对话")
        verdict["is_meta"] = True
    return verdict


async def with_llm_retry(
    coro_fn: Callable[[], Awaitable[T]],
    timeout: float = 60.0,
    max_retries: int = 3,
    non_retryable: tuple = (),
) -> T:
    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        try:
            async with asyncio.timeout(timeout):
                return await coro_fn()
        except asyncio.TimeoutError:
            last_error = LLMTimeoutError(
                f"Attempt {attempt + 1}/{max_retries} timeout after {timeout}s"
            )
            logger.warning("LLM timeout, attempt %d/%d", attempt + 1, max_retries)
            if attempt == max_retries - 1:
                raise last_error
            await asyncio.sleep(2.0 * (attempt + 1))
        except Exception as e:
            last_error = e
            if non_retryable and isinstance(e, non_retryable):
                logger.error("LLM non-retryable (business error): %s", e)
                raise
            if not is_retryable_error(e):
                logger.error("LLM non-retryable error (no retry): %s", e)
                raise
            logger.error("LLM error, attempt %d/%d: %s", attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                raise
            retry_after = _extract_retry_after(e)
            if retry_after is not None:
                wait_time = min(retry_after, 60.0)
                logger.info("LLM rate limited, waiting %ss (retry_after)", wait_time)
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(1.0 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"with_llm_retry exhausted with no attempt made (max_retries={max_retries!r})")
