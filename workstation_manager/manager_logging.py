from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import uuid

from .discovery import redact_sensitive_text


LOGGER_NAME = "workstation_manager"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_sensitive_text(rendered)[0]


def configure_manager_logging(
    path: Path, level: str = "INFO", max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 每个 app 实例拥有独立 handler；一个测试/嵌入实例关闭时不会拆掉另一实例的日志。
    logger = logging.getLogger(f"{LOGGER_NAME}.{uuid.uuid4().hex}")
    logger.setLevel(getattr(logging, level))
    logger.propagate = False
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
