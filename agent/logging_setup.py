"""Logging configuration: console + rotating file, per-message request id,
and a LangGraph node wrapper. See specs/logging-observability.md."""

import functools
import inspect
import json
import logging
import sys
import time
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s.%(funcName)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "googleapiclient", "google.auth", "google_genai", "langsmith", "asyncio")
_HANDLER_MARK = "_study_copilot_handler"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", "-"),
            "logger": record.name,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(level: str, log_format: str, log_dir: Path) -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(h)
        h.close()

    formatter = JsonFormatter() if log_format == "json" else logging.Formatter(_TEXT_FORMAT, _DATE_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    file_error = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        )
    except OSError as e:
        file_error = e

    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(RequestIdFilter())
        setattr(h, _HANDLER_MARK, True)
        root.addHandler(h)
    root.setLevel(level)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    if file_error is not None:
        logger.warning("file logging disabled: %s", file_error)


def log_node(fn):
    """Wraps a LangGraph node: logs duration on success, logs the traceback and
    re-raises on failure, and treats interrupt/bubble-up control flow as a
    pause, not an error. Sync stays sync, async stays async."""
    name = fn.__name__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except GraphBubbleUp:
                logger.info("node=%s paused", name)
                raise
            except Exception:
                logger.exception("node=%s failed", name)
                raise
            logger.info("node=%s ok in %.2fs", name, time.monotonic() - start)
            return result

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except GraphBubbleUp:
                logger.info("node=%s paused", name)
                raise
            except Exception:
                logger.exception("node=%s failed", name)
                raise
            logger.info("node=%s ok in %.2fs", name, time.monotonic() - start)
            return result

    return wrapper
