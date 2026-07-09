from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from nexus.config import NexusConfig, get_settings
from nexus.context import get_request_id

_NEXUS_HANDLER_ATTR: str = "_nexus_handler"


def _mark_handler(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _NEXUS_HANDLER_ATTR, True)
    return handler


class NexusFormatter(logging.Formatter):
    default_format: str = "%(asctime)s [%(levelname)s] %(name)s [req_id=%(request_id)s]: %(message)s"
    detail_format: str = (
        "%(asctime)s [%(levelname)s] %(name)s "
        "[req_id=%(request_id)s] [%(filename)s:%(lineno)d]: %(message)s"
    )

    def __init__(self, detail: bool = False) -> None:
        super().__init__(detail and self.detail_format or self.default_format)

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = get_request_id() or "-"
        return super().format(record)


def setup_logging(
    config: Optional[NexusConfig] = None,
    app_name: Optional[str] = None,
) -> logging.Logger:
    cfg: NexusConfig = config or get_settings()
    log_cfg = cfg.logging

    log_dir: Path = Path(log_cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger: logging.Logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))

    for handler in list(root_logger.handlers):
        if getattr(handler, _NEXUS_HANDLER_ATTR, False):
            root_logger.removeHandler(handler)

    formatter: NexusFormatter = NexusFormatter(detail=cfg.debug)

    if log_cfg.console:
        console_handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))
        _mark_handler(console_handler)
        root_logger.addHandler(console_handler)

    app_log_path: Path = log_dir / f"{app_name or cfg.app_name}.log"
    file_handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        app_log_path,
        when="midnight",
        interval=1,
        backupCount=log_cfg.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    _mark_handler(file_handler)
    root_logger.addHandler(file_handler)

    error_log_path: Path = log_dir / f"{app_name or cfg.app_name}_error.log"
    error_handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        error_log_path,
        when="midnight",
        interval=1,
        backupCount=log_cfg.retention_days,
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)
    _mark_handler(error_handler)
    root_logger.addHandler(error_handler)

    for noisy_logger in ("uvicorn", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    app_logger: logging.Logger = logging.getLogger(app_name or cfg.app_name)
    app_logger.info(
        "Logging initialized: level=%s, dir=%s", log_cfg.level, log_dir
    )
    return app_logger


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(name)


class _StdlibToLoguruHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            from loguru import logger as _loguru_logger
            level: str = record.levelname
            try:
                level = _loguru_logger.level(record.levelname).name
            except (ValueError, TypeError):
                pass
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            _loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
        except ImportError:
            pass


def setup_loguru(
    app_name: str = "app",
    log_level: str = "INFO",
    log_dir: str = "logs",
    retention_days: int = 3,
    bridge_stdlib: bool = True,
) -> None:
    try:
        from loguru import logger
    except ImportError:
        setup_logging()
        return

    import os as _os
    from pathlib import Path as _Path

    _Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()

    logger.add(
        _os.sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        enqueue=True,
    )

    logger.add(
        f"{log_dir}/{app_name}-{{time:YYYY-MM-DD}}.log",
        rotation="00:00",
        retention=f"{retention_days} days",
        compression="gz",
        encoding="utf-8",
        level=log_level,
        enqueue=True,
        filter=lambda record: record["level"].no < 40,
    )

    logger.add(
        f"{log_dir}/{app_name}-error-{{time:YYYY-MM-DD}}.log",
        rotation="00:00",
        retention=f"{retention_days} days",
        compression="gz",
        encoding="utf-8",
        level="ERROR",
        enqueue=True,
    )

    if bridge_stdlib:
        logging.basicConfig(handlers=[_StdlibToLoguruHandler()], level=logging.INFO, force=True)
        for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "sqlalchemy", "ironman"):
            _log = logging.getLogger(_name)
            _log.handlers = [_StdlibToLoguruHandler()]
            _log.propagate = False

    logger.info(f"Loguru logging initialized: app={app_name}, level={log_level}, dir={log_dir}")
