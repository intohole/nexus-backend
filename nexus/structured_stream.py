"""结构化 JSON 数组流式生成 — LLM 单次直出 text/action 交错数组的增量解析与流式透传。

协议:
    [{"type":"text","content":"台词"},
     {"type":"action","name":"wb_draw","params":{...}},
     {"type":"text","content":"接下来..."}]

对外入口 structured_stream 复用 resilient_stream 底座,逐 chunk 增量解析:
完整落定的 action 元素即时 emit;尾部未闭合的 text 元素仅透传 content 增量,
保证动作与台词交错实时推送;数组未闭合或非法输出时自动降级为纯文本。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from nexus.logging import get_logger
from nexus.resilient_llm import resilient_stream

logger = get_logger("nexus.structured_stream")

_SCHEMA_KEY_START = re.compile(r'^"?type"?\s*:\s*"(text|action)"')
_SCHEMA_KEY_MID = re.compile(r'^"(content|name|params|action_id)"\s*:')


@dataclass
class StructuredParserState:
    buffer: str = ""
    json_started: bool = False
    emitted_count: int = 0
    partial_text_len: int = 0
    is_done: bool = False
    item_index: int = 0


@dataclass
class StructuredParseResult:
    new_items: List[Dict[str, Any]] = field(default_factory=list)
    text_delta: str = ""
    is_done: bool = False


def create_structured_parser() -> StructuredParserState:
    return StructuredParserState()


def _split_top_level(buffer: str) -> Tuple[List[str], bool, int]:
    """扫描顶层数组,拆分完整元素文本。

    返回 (完整元素列表, 是否已闭合, 未闭合尾部对象的起点索引或-1)。
    顶层 `[` 记为 depth=1,数组内顶层元素 `{...}` 是唯一在 depth==1 时
    起始的对象;字符级平衡扫描正确处理字符串转义与嵌套。
    """
    items: List[str] = []
    depth = 0
    in_string = False
    escaped = False
    start = -1
    closed = False
    tail_start = -1
    for idx, ch in enumerate(buffer):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "{":
            if depth == 1:
                start = idx
                tail_start = idx
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 1 and start >= 0:
                items.append(buffer[start : idx + 1])
                start = -1
                tail_start = -1
        elif ch == "]" and depth > 0:
            depth -= 1
            if depth == 0:
                closed = True
                tail_start = -1
                break
    return items, closed, tail_start


def _loads_item(item: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(item)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_tail_text(content: str) -> Optional[str]:
    """从含未闭合 text 元素的尾部片段中提取 content 的可见部分。

    仅处理 type==text;`\"` 转义还原以便流式透传原始文本;content 未闭合
    (引号未出现)时也返回,实现"打字机"式增量。
    """
    key = '"content"'
    idx = content.rfind(key)
    if idx == -1:
        return None
    type_idx = content.rfind('"type"', 0, idx)
    if type_idx != -1:
        m = re.search(r':\s*"(\w+)"', content[type_idx:idx])
        if m and m.group(1) != "text":
            return None
    colon = content.find(":", idx + len(key))
    if colon == -1:
        return None
    quote = content.find('"', colon + 1)
    if quote == -1:
        return None
    raw = content[quote + 1 :]
    buf: List[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == '"':
                buf.append('"')
                i += 2
                continue
            if nxt == "\\":
                buf.append("\\")
                i += 2
                continue
        if ch == '"':
            return "".join(buf)
        buf.append(ch)
        i += 1
    return "".join(buf)


def parse_structured_chunk(chunk: str, state: StructuredParserState) -> StructuredParseResult:
    result = StructuredParseResult()
    if state.is_done:
        return result
    state.buffer += chunk

    if not state.json_started:
        bracket = state.buffer.find("[")
        if bracket == -1:
            return result
        state.buffer = state.buffer[bracket:]
        state.json_started = True

    items, closed, tail_start = _split_top_level(state.buffer)
    if closed:
        for i in range(state.emitted_count, len(items)):
            parsed = _loads_item(items[i])
            if parsed is None:
                continue
            if parsed.get("type") == "text":
                content = str(parsed.get("content", ""))
                if i == state.emitted_count and state.partial_text_len > 0:
                    delta = content[state.partial_text_len :]
                else:
                    delta = content
                if delta:
                    result.text_delta += delta
            else:
                result.new_items.append(parsed)
        state.emitted_count = len(items)
        state.partial_text_len = 0
        state.is_done = True
        result.is_done = True
        return result

    complete_up_to = len(items)
    for i in range(state.emitted_count, complete_up_to):
        parsed = _loads_item(items[i])
        if parsed is None:
            continue
        if parsed.get("type") == "text":
            content = str(parsed.get("content", ""))
            if i == state.emitted_count and state.partial_text_len > 0:
                delta = content[state.partial_text_len :]
            else:
                delta = content
            state.partial_text_len = 0
            if delta:
                result.text_delta += delta
        else:
            result.new_items.append(parsed)
    state.emitted_count = complete_up_to

    if tail_start > -1:
        pending = _extract_tail_text(state.buffer[tail_start:]) or ""
        if pending and len(pending) > state.partial_text_len:
            result.text_delta += pending[state.partial_text_len :]
            state.partial_text_len = len(pending)
    return result


def _looks_like_structured_fragment(raw: str) -> bool:
    trimmed = raw.strip()
    if not trimmed:
        return False
    if _SCHEMA_KEY_START.match(trimmed) or _SCHEMA_KEY_MID.match(trimmed):
        return True
    first = trimmed[0]
    if first not in "[{":
        return False
    return trimmed[1:2] == "" or trimmed[1] in "[{\"]}"


def _extract_clean_structured_text(raw: str) -> List[str]:
    trimmed = raw.strip()
    if not trimmed.startswith(("{", "[")):
        return []
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    if not items or not all(isinstance(it, dict) and "type" in it for it in items):
        return []
    texts = [
        str(it.get("content", "")).strip()
        for it in items
        if it.get("type") == "text" and isinstance(it.get("content"), str)
    ]
    return [t for t in texts if t]


def finalize_structured_parser(state: StructuredParserState) -> StructuredParseResult:
    result = StructuredParseResult()
    if state.is_done:
        return result
    content = state.buffer.strip()
    if not content:
        state.is_done = True
        result.is_done = True
        return result

    if not state.json_started:
        texts = _extract_clean_structured_text(content)
        if texts:
            result.text_delta = "\n".join(texts)
        elif not _looks_like_structured_fragment(content):
            result.text_delta = content
    else:
        items, closed, tail_start = _split_top_level(state.buffer)
        for item in items[state.emitted_count :]:
            parsed = _loads_item(item)
            if parsed is not None and parsed.get("type") != "text":
                result.new_items.append(parsed)
        if tail_start > -1 and not items:
            tail = state.buffer[tail_start:]
            parsed = _loads_item(tail)
            if parsed is not None and parsed.get("type") != "text":
                result.new_items.append(parsed)
    state.is_done = True
    result.is_done = True
    return result


async def structured_stream(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.4,
    max_tokens: int = 2000,
    alias: str = "default",
    namespace: Optional[str] = None,
    task_type: Optional[str] = None,
    on_item: Optional[Callable[[int, Dict[str, Any]], Awaitable[None]]] = None,
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """流式生成结构化 text/action 交错 JSON 数组(复用 resilient_stream 底座)。

    yield: ("item", {"index": n, "item": {...}})  action 元素完整落定
           ("text", {"content": "..."})           text 增量
           ("done", {"item_count": N})            数组闭合或流结束
           ("error", {"message": "..."})          底层异常
    """
    state = create_structured_parser()
    text_index = 0
    item_count = 0
    try:
        async for token in resilient_stream(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            alias=alias,
            namespace=namespace,
            task_type=task_type,
        ):
            result = parse_structured_chunk(token, state)
            for item in result.new_items:
                item_count += 1
                yield "item", {"index": item_count - 1, "item": item}
                if on_item is not None:
                    try:
                        await on_item(item_count - 1, item)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("on_item callback failed: %s", item_count - 1)
            if result.text_delta:
                text_index += 1
                yield "text", {"index": text_index, "content": result.text_delta}
            if result.is_done:
                break
    except Exception as exc:
        logger.warning("structured_stream %s failed: %s", alias, exc)
        yield "error", {"message": str(exc)}
        return

    if not state.is_done:
        final = finalize_structured_parser(state)
        for item in final.new_items:
            item_count += 1
            yield "item", {"index": item_count - 1, "item": item}
        if final.text_delta:
            text_index += 1
            yield "text", {"index": text_index, "content": final.text_delta}
    yield "done", {"item_count": item_count}