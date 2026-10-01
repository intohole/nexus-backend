"""setup_loguru 桥接参数回归。"""
import logging

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
