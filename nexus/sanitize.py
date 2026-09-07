"""Agent 输出卫生层：剥离模型混入 final_answer 的推理段与工具 trace，防止思维链泄漏。

提供两个入口：
- sanitize_agent_output(text): 对整段文本做静态剥离
- sanitize_text_stream(chunks): 流式过滤（先缓冲推理段，遇到正文信号后放行）
"""
from __future__ import annotations

import re
from typing import AsyncIterator, List, Tuple

_REASON_PREFIX_RE: re.Pattern[str] = re.compile(
    r"^\s*(用户(明确|现在|刚才|这次)?(询问|要求|提出|需要|想知道|想了解)"
    r"|根据(用户|您|以上|所述|分析)|首先，?我|我需要|请先|分析(用户)?请求"
    r"|当前问题|任务(是|为)|我们要|我来(整理|分析|回答|调用))"
)
_TOOL_VERB_RE: re.Pattern[str] = re.compile(
    r"调用|查询|获取|分析|检索|解析|搜索|查询|接口|工具|返回结果|数据如下|应该先用|需要先|已调用"
)
_TOOL_TRACE_RE: re.Pattern[str] = re.compile(
    r"(功能|工具|能力|接口)[\u4e00-\u9fa5A-Za-z0-9_()（）]{0,20}(专门|主要)?用于"
    r"|(专门|主要)?用于(获取|查询|展示|处理)[\u4e00-\u9fa5A-Za-z0-9_()（）]{0,20}(功能|工具|能力|数据)"
    r"|\b[a-zA-Z][a-zA-Z0-9_]{2,30}\.[a-zA-Z]\w+\b"
)
_DEFINITELY_TRACE_RE: re.Pattern[str] = re.compile(
    r"(功能|工具|能力|接口)[\u4e00-\u9fa5A-Za-z0-9_()（）]{0,20}(专门|主要)?用于"
    r"|(专门|主要)?用于(获取|查询|展示|处理)"
)
_TAIL_HINTS: tuple[str, ...] = (
    "最终答案",
    "综上",
    "因此",
    "所以，",
    "所以:",
    "所以：",
    "以下是",
    "具体来说",
    "简言之",
    "结果如下",
    "答案是",
    "回答：",
    "回答:",
    "综上所述",
)

def strip_known_tool_traces(text: str, tool_names: list[str]) -> str:
    """剥离正文中混入的内部工具调用痕迹(如 get_stock_price招商银行(SH600036))。

    命中白名单工具名时删除工具名及其紧贴的参数串/动作词，保留行内正常正文。
    """
    if not text or not tool_names:
        return text
    names: list[str] = [n for n in tool_names if n]
    if not names:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        cleaned: str = line
        for name in names:
            while True:
                start: int = cleaned.find(name)
                if start < 0:
                    break
                head: str = cleaned[:start]
                tail: str = cleaned[start + len(name):]
                m = re.match(r"[^，,。.、；;:：\s]{0,40}", tail)
                tail_clean: str = tail[m.end():] if m else tail
                cleaned = (head + tail_clean).strip()
        cleaned = cleaned.strip()
        if cleaned:
            out.append(cleaned.lstrip("，,。.、；;:： ").strip())
    joined: str = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _strip_trace_lines(text: str) -> str:
    out: List[str] = []
    for line in text.split("\n"):
        stripped: str = line.strip()
        if not stripped:
            continue
        if _DEFINITELY_TRACE_RE.search(stripped):
            continue
        if _REASON_PREFIX_RE.match(stripped) and _TOOL_VERB_RE.search(stripped):
            continue
        if _REASON_PREFIX_RE.match(stripped) and _TOOL_TRACE_RE.search(line):
            continue
        out.append(line)
    return "\n".join(out)


def sanitize_agent_output(text: str) -> str:
    if not text:
        return text
    cleaned: str = text
    cleaned = _strip_trace_lines(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _boost(body: str) -> Tuple[str, bool]:
    for hint in _TAIL_HINTS:
        idx: int = body.find(hint)
        if idx > 0:
            return body, True
    return body, False


async def sanitize_text_stream(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """流式过滤：缓冲推理段候选，检测到正文信号后输出清洗后的缓冲与后续原文。"""
    buf: List[str] = []
    released: bool = False
    async for chunk in chunks:
        if released:
            yield chunk
            continue
        buf.append(chunk)
        body: str = "".join(buf)
        _body, hit = _boost(body)
        if hit:
            cleaned: str = sanitize_agent_output(body)
            yield cleaned
            released = True
            buf = []
    if not released:
        yield sanitize_agent_output("".join(buf))