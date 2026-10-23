"""LLM 通用工具：超时/可重试判定、重试等待解析与 JSON 提取解析。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger("nexus.llm_utils")

T = TypeVar("T")

# parse_llm_json(fallback=) 的「未传」哨兵：与显式传 fallback=None（失败返回 None）区分
_UNSET = object()


# P1: 不可重试的 HTTP 状态码（4xx 客户端错误，除 429 限流）
_NON_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("400", "401", "403", "404", "422")
# P1: 可重试的 HTTP 状态码（429 限流、5xx 服务端错误）
_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("429", "500", "502", "503", "504")


class LLMTimeoutError(Exception):
    """LLM 调用超时异常。"""
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


def find_balanced_json(content: str) -> Optional[str]:
    """从文本中提取第一个平衡的JSON对象（括号匹配法，非法候选自动跳过）。

    单一实现在 ironman.validation.structured（与 ironman extract_json 共用一份算法），
    函数内惰性导入：nexus 已模块级依赖 ironman，此向保持依赖方向不变且不成环。
    """
    from ironman.validation.structured import find_balanced_json as _find_balanced_impl
    return _find_balanced_impl(content)


def parse_llm_json(raw: str, fallback: object = _UNSET) -> dict[str, object]:
    """解析 LLM 输出为 JSON 对象，逐级降级修复。

    失败时：未传 fallback 返回 {"raw_response": text} 哨兵（既有语义）；
    显式传 fallback（含 None）则返回该值——业务侧「失败返回默认值/None」
    的 shim 样板单源化入口。
    """
    text = strip_code_fence(raw)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    candidate = find_balanced_json(text)
    if candidate:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidate = m.group()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        cleaned = re.sub(r",\s*}", "}", candidate)
        cleaned = re.sub(r",\s*]", "]", cleaned)
        cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
        for _ in range(3):
            merged = re.sub(r'"([^"]*)"\s*:\s*"([^"]*)"\s*:\s*"', r'"\1":"\2：', cleaned)
            if merged == cleaned:
                break
            cleaned = merged
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        fixed = re.sub(r"(\w+)\s*:", r'"\1":', cleaned)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_block = parts[1].split("```")[0]
            try:
                result = json.loads(json_block.strip())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    logger.warning("JSON parse failed after all attempts: %s", text[:300])
    if fallback is not _UNSET:
        return fallback  # type: ignore[return-value]
    return {"raw_response": text}

def is_llm_json_error(result: object) -> bool:
    """判断 parse_llm_json 未传 fallback 时的返回值是否为解析失败哨兵。

    哨兵 {"raw_response": text} 是个合法 dict——消费方直接 .get("field")
    会静默拿到空值（verseCraft r9 生产实锤：结构化记忆管线整条空军、
    追踪/图谱 0 落库，零异常零告警）。关键结构化消费方必须在取字段前
    用本函数单源检测，而不是各自手写 `"raw_response" in result`。
    """
    return isinstance(result, dict) and set(result.keys()) == {"raw_response"}

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

def _parse_truncated_array(text: str) -> Optional[object]:
    """截断 JSON 数组的确定性修复：按最后完整元素边界闭合回收。

    LLM 长列表输出被 max_tokens 掐断（缺右括号）是高频形态——完整元素仍可回收，
    比整体丢弃保住大部分结果（goldenStock 私有版语义收编，harness r7 层2 收归）。
    边界用引号态扫描判定（对象元素=`}`回深度1、字符串元素=闭合引号），
    截断在字符串中段/数字元素后不回收，宁缺勿错。
    """
    start = text.find("[")
    if start < 0:
        return None
    raw = text[start:]
    boundaries: list[int] = []
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if depth == 1:
                    boundaries.append(i)
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                break
            if depth == 1:
                boundaries.append(i)
    if not boundaries:
        return None
    try:
        repaired = json.loads(raw[: boundaries[-1] + 1] + "]")
    except json.JSONDecodeError:
        return None
    return repaired if isinstance(repaired, list) else None


def parse_llm_json_lenient(raw: str) -> Optional[object]:
    """LLM 输出的宽容解析：支持顶层对象或数组，逐级降级提取，全部失败返回 None。

    与 parse_llm_json 的分工：parse_llm_json 面向"必须是 JSON 对象"的结构化抽取
    （失败返回 {"raw_response": ...} 哨兵）；本函数面向"对象或数组皆可"的
    自由输出（如研究发现列表），失败返回 None 由调用方跳过本轮。
    降级梯子末级为截断修复（`_parse_truncated_array`）：输出被 max_tokens 掐断、
    缺右括号时按最后完整元素闭合回收。
    """
    text = strip_code_fence(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    repaired = _parse_truncated_array(text)
    if repaired is not None:
        return repaired
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


def parse_llm_json_or(raw: str, default: T) -> T:
    """lenient 解析的带默认值版本：解析失败（含结果为 null）返回 default。

    收敛各业务 `result = parse_llm_json_lenient(raw); if result is None: result = default` 样板。
    """
    result = parse_llm_json_lenient(raw)
    return result if result is not None else default


def parse_json_column(value: object, default: T) -> T:
    """DB JSON 列/消息参数的读取面：dict/list 透传，str 容错解析，其余返回 default。

    与 parse_llm_json 的差异：非字符串输入不进修复链——数据库 JSON 列、
    跨服务消息参数「通常已是合法 JSON」，只需透传 + 兜底，不需要围栏剥离。
    收敛各业务 safe_jsonloads/_safe_json 手写 try/except 样板。
    """
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    result = parse_llm_json_lenient(value)
    return result if result is not None else default


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


def strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1])
        return "\n".join(lines[1:])
    return text
