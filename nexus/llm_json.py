"""LLM 输出 JSON 提取与解析单源：围栏剥离、降级解析梯子、截断修复与列读取面。

自 nexus.llm_utils 拆出（该文件超限，JSON 解析族自成边界）；nexus.llm_utils
保留同名 re-export，新旧导入路径等价。ironman JSON 提取算法的委托关系不变。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional, TypeVar

logger = logging.getLogger("nexus.llm_utils")

T = TypeVar("T")

# parse_llm_json(fallback=) 的「未传」哨兵：与显式传 fallback=None（失败返回 None）区分
_UNSET = object()


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


def strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1])
        return "\n".join(lines[1:])
    return text
