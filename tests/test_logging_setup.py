"""setup_loguru 桥接参数回归。"""
import logging

from nexus.context import set_request_context
from nexus.logging import _StdlibToLoguruHandler, setup_loguru


def test_extra_bridge_loggers_attach_handler(tmp_path):
    root_handlers = list(logging.root.handlers)
    try:
        setup_loguru(
            app_name="nexus-test-extra-bridge",
            log_level="INFO",
            log_dir=str(tmp_path),
            extra_bridge_loggers=("aiosqlite",),
        )
        assert isinstance(logging.getLogger("aiosqlite").handlers[0], _StdlibToLoguruHandler)
        assert logging.getLogger("aiosqlite").propagate is False
        assert isinstance(logging.getLogger("uvicorn").handlers[0], _StdlibToLoguruHandler)
    finally:
        logging.basicConfig(handlers=root_handlers, force=True)


def test_loguru_sink_carries_request_context(tmp_path):
    from loguru import logger as loguru_logger

    root_handlers = list(logging.root.handlers)
    set_request_context(request_id="tr-abc", user_id="42")
    try:
        setup_loguru(
            app_name="nexus-test-ctx",
            log_level="INFO",
            log_dir=str(tmp_path),
            extra_bridge_loggers=("stdlib-bridge-probe",),
        )
        logging.getLogger("stdlib-bridge-probe").info("bridged line")
        loguru_logger.info("native line")
        loguru_logger.complete()
    finally:
        set_request_context(request_id="", user_id="")
        loguru_logger.remove()
        logging.basicConfig(handlers=root_handlers, force=True)
    app_log = next(p for p in tmp_path.iterdir() if p.name.startswith("nexus-test-ctx") and "error" not in p.name)
    content = app_log.read_text(encoding="utf-8")
    assert "[req_id=tr-abc uid=42]" in content
    assert "bridged line" in content
    assert "native line" in content


def test_loguru_sink_context_defaults_dash(tmp_path):
    from loguru import logger as loguru_logger

    setup_loguru(
        app_name="nexus-test-ctx-default",
        log_level="INFO",
        log_dir=str(tmp_path),
        bridge_stdlib=False,
    )
    try:
        loguru_logger.info("plain line")
        loguru_logger.complete()
    finally:
        loguru_logger.remove()
    app_log = next(p for p in tmp_path.iterdir() if p.name.startswith("nexus-test-ctx-default") and "error" not in p.name)
    assert "[req_id=- uid=-]" in app_log.read_text(encoding="utf-8")
