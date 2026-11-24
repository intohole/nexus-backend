"""stdlib→loguru 桥 req_id 注入守底（S200·P8 库级根治）。

P8 定谳（r243）：stdlib logging 经 callHandlers 转发链整体丢请求上下文
（usercenter 业务行+access 行 req_id 恒 -）。修复=桥 emit 时 .bind 注入
contextvar（req_id/uid），宿主未配 patcher 时也带 id；配了 patcher 的
nexus 全链路为同值覆盖，行为不变。
"""
from __future__ import annotations

import logging

import pytest
from loguru import logger as loguru_logger

from nexus.context import set_request_context
from nexus.logging import _StdlibToLoguruHandler


class _Sink:
    def __init__(self) -> None:
        self.records: list[object] = []

    def __call__(self, message: object) -> None:
        self.records.append(message.record)


def test_bridge_binds_contextvar_without_patcher(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _Sink()
    handler_id = loguru_logger.add(sink, format="{message}")
    try:
        stdlib_logger = logging.getLogger("p8.probe")
        stdlib_logger.setLevel(logging.INFO)
        stdlib_logger.handlers = [_StdlibToLoguruHandler()]
        stdlib_logger.propagate = False

        set_request_context(request_id="p8req123")
        stdlib_logger.info("业务动作测试")

        assert sink.records, "loguru sink received nothing"
        assert sink.records[0]["extra"]["req_id"] == "p8req123"
    finally:
        loguru_logger.remove(handler_id)
        set_request_context(request_id="")


def test_bridge_dash_when_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _Sink()
    handler_id = loguru_logger.add(sink, format="{message}")
    try:
        stdlib_logger = logging.getLogger("p8.probe.noctx")
        stdlib_logger.setLevel(logging.INFO)
        stdlib_logger.handlers = [_StdlibToLoguruHandler()]
        stdlib_logger.propagate = False

        set_request_context(request_id="")
        stdlib_logger.info("无上下文动作")

        assert sink.records
        assert sink.records[0]["extra"]["req_id"] == "-"
    finally:
        loguru_logger.remove(handler_id)
