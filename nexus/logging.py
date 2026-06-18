from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from nexus.config import NexusConfig, get_settings
from nexus.context import get_request_id


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
        root_logger.removeHandler(handler)

    formatter: NexusFormatter = NexusFormatter(detail=cfg.debug)

    if log_cfg.console:
        console_handler: logging.StreamHandler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))
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
