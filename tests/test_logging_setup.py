"""setup_loguru 桥接参数回归。"""
import logging
import os

from nexus.context import set_request_context
from nexus.logging import _StdlibToLoguruHandler, setup_logging, setup_loguru


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


def test_setup_loguru_env_overrides_args(tmp_path, monkeypatch):
    from loguru import logger as loguru_logger

    injected_dir = tmp_path / "injected"
    monkeypatch.setenv("LOGGING__DIR", str(injected_dir))
    monkeypatch.setenv("LOGGING__RETENTION_DAYS", "not-a-number")
    root_handlers = list(logging.root.handlers)
    try:
        setup_loguru(
            app_name="nexus-test-env",
            log_level="INFO",
            log_dir=str(tmp_path / "arg-dir"),
            bridge_stdlib=False,
        )
        loguru_logger.info("env routed line")
        loguru_logger.complete()
    finally:
        loguru_logger.remove()
        logging.basicConfig(handlers=root_handlers, force=True)
    files = [p.name for p in injected_dir.iterdir()]
    assert any(n.startswith("nexus-test-env-") and "error" not in n for n in files)
    assert any("error" in n for n in files)
    assert not (tmp_path / "arg-dir").exists()


def test_setup_loguru_unwritable_dir_falls_back(tmp_path, monkeypatch):
    from loguru import logger as loguru_logger

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("LOGGING__DIR", str(locked / "sub"))
    monkeypatch.chdir(tmp_path)
    root_handlers = list(logging.root.handlers)
    try:
        setup_loguru(app_name="nexus-test-fallback", log_level="INFO", bridge_stdlib=False)
        loguru_logger.info("fallback routed line")
        loguru_logger.complete()
    finally:
        loguru_logger.remove()
        logging.basicConfig(handlers=root_handlers, force=True)
        locked.chmod(0o700)
    fallback_files = [p.name for p in (tmp_path / "logs").iterdir()]
    assert any(n.startswith("nexus-test-fallback-") for n in fallback_files)


def test_setup_logging_skips_handlers_when_bridged(tmp_path):
    from nexus.config import NexusConfig

    root_handlers = list(logging.root.handlers)
    try:
        logging.root.handlers = [_StdlibToLoguruHandler()]
        cfg = NexusConfig()
        cfg.logging.dir = str(tmp_path / "should-not-be-created")
        setup_logging(cfg, app_name="nexus-test-bridged")
        marked = [h for h in logging.root.handlers if getattr(h, "_nexus_handler", False)]
        assert marked == []
        assert not (tmp_path / "should-not-be-created").exists()
    finally:
        logging.basicConfig(handlers=root_handlers, force=True)
