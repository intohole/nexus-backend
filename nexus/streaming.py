"""SSE 流式响应助手 — 统一对话流式输出协议（facade：协议原语 + v2 机制 re-export）。

所有 app 的 chat_stream 端点应使用 `sse_chat_stream` / `sse_response` 包装异步生成器，
避免每个 app 各写一套 SSE 协议。

事件格式（遵循 SSE 规范）：
    data: {"event": "delta", "content": "..."}\n\n
    data: {"event": "done", "content": "完整文本"}\n\n
    data: {"event": "error", "error": "..."}\n\n

统一事件 schema（推荐用 sse_event_dict）：
    data: {"type": "delta", "content": "..."}\n\n
    data: {"type": "done", "content": "完整文本"}\n\n
    data: {"type": "error", "message": "..."}\n\n

消息内组件事件（组件协议，前端 nux-ai-chat 自动路由渲染）：
    data: {"type": "widget", "id": "w1", "widget": "table", "title": "技术指标",
           "data": {"summary": {"信号": "买入"}, "columns": ["指标", "数值"], "rows": [...]}}\n\n
    data: {"type": "widget_update", "id": "w1", "data": {...}}\n\n
    widget 取值: table(数据表) / cards(卡片列表) / steps(步骤时间线, 支持 percent+status 呈现任务进度) /
                 related(相关追问, data.items[{text,value}], 点击自动追问) /
                 choice(选项选择, data.options[{label,value,recommended}]) /
                 feedback(赞踩反馈) /
                 form(表单收集, data.fields[{key,label,type,required,options}], 提交回传字段值) /
                 chart(图表, data.chart=bar|line|pie + categories/series, 或直接传 echarts option) /
                 confirm(危险操作二次确认, data.message/detail/items/warning, 回传 true|false)
    widget_update 用于流式填充或更新已下发组件的 data（按 id 定位）；task 进度用
    {"type":"widget_update","id":"w1","data":{"percent":60,"status":"running","steps":[...]}}。

分层（r158 模块级拆分，全舰队导入面 `nexus.streaming.*` 不变）：
    _streaming_primitives（协议原语）← _streaming_sse（v2 流水线机制）← 本 facade。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

from nexus._streaming_primitives import (  # noqa: F401
    SSE_DONE,
    SSE_HEADERS,
    sse_data_line,
    sse_event_dict,
    sse_raw_frame,
    sse_response,
)
from nexus._streaming_sse import (  # noqa: F401
    DEFAULT_SSE_QUEUE_SIZE,
    DRAIN_TIMEOUT_SECONDS,
    _DRAIN_TASKS,
    _STREAM_SENTINEL,
    _drain_after_disconnect,
    _sse_generator_v2,
    _teardown_pump,
    sse_chat_stream_v2,
    with_disconnect_check,
)

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _openai_sse_payloads(raw: str):
    """逐帧产出 OpenAI 兼容 SSE 文本中的已解析 JSON 载荷（全文回读变体的共用游标）。"""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def openai_sse_content(raw: str) -> str:
    """解析 OpenAI 兼容 SSE 全文，拼接全部 content 增量（openai_sse_deltas 的同步全文版）。

    遍历所有 choices（n>1 时 output token 口径需涵盖全部）；用于拿到完整响应
    后回读统计/落库的场景；坏帧静默跳过。
    """
    parts: list[str] = []
    for chunk in _openai_sse_payloads(raw):
        for choice in chunk.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(content)
    return "".join(parts)


def openai_sse_usage(raw: str) -> Optional[dict[str, Any]]:
    """提取 OpenAI 兼容 SSE 的 usage 帧（stream_options.include_usage 的末帧）。

    返回首个非空 usage dict，无则 None；坏帧静默跳过。
    """
    for chunk in _openai_sse_payloads(raw):
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            return usage
    return None


async def openai_sse_deltas(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """解析 OpenAI 兼容 SSE 行流，产出 content 增量（客户端侧，与 emit 侧 sse_* 对偶）。

    lines 为按行迭代的异步流（如 httpx resp.aiter_lines()）；提取 data: 帧
    delta.content，[DONE] 哨兵终止，坏帧静默跳过。
    """
    async for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue
        content = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if content:
            yield content


class ThinkStreamFilter:
    """流式 <think> 标签过滤器：增量喂入 content，输出剥离思维链后的文本。

    跨标签边界的部分标签会暂存，直到可判定后输出；流结束时调用 flush()。
    """

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._inside:
                idx = self._buf.find(THINK_CLOSE)
                if idx == -1:
                    keep = len(THINK_CLOSE) - 1
                    if len(self._buf) > keep:
                        self._buf = self._buf[-keep:]
                    break
                self._inside = False
                self._buf = self._buf[idx + len(THINK_CLOSE):]
            else:
                o = self._buf.find(THINK_OPEN)
                c = self._buf.find(THINK_CLOSE)
                if o != -1 and (c == -1 or o < c):
                    if o > 0:
                        out.append(self._buf[:o])
                    self._inside = True
                    self._buf = self._buf[o + len(THINK_OPEN):]
                elif c != -1:
                    if c > 0:
                        out.append(self._buf[:c])
                    self._buf = self._buf[c + len(THINK_CLOSE):]
                else:
                    keep = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1
                    if len(self._buf) > keep:
                        out.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    break
        return "".join(out)

    def flush(self) -> str:
        if self._inside:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


__all__ = [
    "SSE_HEADERS",
    "SSE_DONE",
    "DEFAULT_SSE_QUEUE_SIZE",
    "DRAIN_TIMEOUT_SECONDS",
    "sse_event_dict",
    "sse_data_line",
    "sse_raw_frame",
    "sse_response",
    "sse_chat_stream_v2",
    "ThinkStreamFilter",
    "openai_sse_deltas",
    "openai_sse_content",
    "openai_sse_usage",
    "with_disconnect_check",
]
