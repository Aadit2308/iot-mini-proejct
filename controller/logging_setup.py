"""Timestamped event logging.

Every event is written twice:
  * logs/irrigation.log  - human readable, one line per event
  * logs/events.jsonl    - machine readable (used by the dashboard log viewer)

Event categories (the ``category`` argument of :func:`log_event`):
  VALID_READING, INVALID_READING, MALFORMED_MESSAGE, MISSING_MESSAGE,
  DELAYED_MESSAGE, TIMEOUT, PUMP_STATE_CHANGE, AUTO_CONTROL,
  CONFIG_UPDATE, CONFIG_REJECTED, RECOVERY, ALERT, SYSTEM
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import DEFAULT_LOG_DIR

LOGGER_NAME = "irrigation"

_WARNING_CATEGORIES = {
    "INVALID_READING",
    "MALFORMED_MESSAGE",
    "MISSING_MESSAGE",
    "DELAYED_MESSAGE",
    "TIMEOUT",
    "CONFIG_REJECTED",
    "ALERT",
}

_jsonl_lock = threading.Lock()
_jsonl_path: Path | None = None


def iso_timestamp(epoch: float | None = None) -> str:
    """RFC 3339 / ISO 8601 timestamp in UTC with milliseconds."""
    dt = (
        datetime.now(timezone.utc)
        if epoch is None
        else datetime.fromtimestamp(epoch, tz=timezone.utc)
    )
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def setup_logging(
    log_dir: Path | str = DEFAULT_LOG_DIR,
    log_name: str = "irrigation",
    console: bool = True,
) -> logging.Logger:
    """Configure the shared logger. Safe to call more than once."""
    global _jsonl_path
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / f"{log_name}.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    logger.propagate = False
    # the controller owns events.jsonl; other processes (e.g. the NETCONF
    # adapter, which runs as root) get their own file
    _jsonl_path = log_dir / ("events.jsonl" if log_name == "irrigation" else f"{log_name}_events.jsonl")
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(category: str, message: str, **data: Any) -> dict:
    """Log one categorized event and return the event record."""
    record = {
        "timestamp": iso_timestamp(),
        "category": category,
        "message": message,
    }
    if data:
        record["data"] = data

    level = logging.WARNING if category in _WARNING_CATEGORIES else logging.INFO
    get_logger().log(level, "[%s] %s", category, message)

    if _jsonl_path is not None:
        line = json.dumps(record, default=str)
        with _jsonl_lock:
            with open(_jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    return record
