"""Structured, privacy-safe logging for portfolio résumé sync.

Logs are emitted to standard error as one JSON object per event with stable
event names and reason codes. Per Task 003 section 9, logs may contain:

- result status and safe reason code;
- source hostname without userinfo, query, or fragment;
- HTTP status;
- byte/page/character counts;
- whether validators were sent or returned (booleans);
- whether raw or normalized content changed (booleans); and
- a created or matched ``resume_version_id``.

Logs and serialized errors must not contain résumé text, excerpts, PDF bytes,
request/response bodies, full headers, ETag values, ``Last-Modified`` values,
a database URL, credentials, a configured URL's userinfo/query/fragment, raw
SQL parameters containing résumé content, or raw extractor/database exception
strings that can echo content or secrets.

The :func:`safe_hostname` helper strips everything but the hostname so a
configured URL can contribute a host to a log line without leaking its path,
query, fragment, or userinfo.
"""

from __future__ import annotations

import json
import logging
import sys
from urllib.parse import urlparse

_LOGGER_NAME = "app.resume"


def safe_hostname(url: str | None) -> str | None:
    """Return only the hostname of ``url`` (no userinfo/query/fragment/path).

    Returns ``None`` when ``url`` is ``None`` or has no parseable hostname, so
    a caller can always include the returned value without leaking more.
    """

    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    return parsed.hostname


def configure_logging(*, verbose: bool = False) -> logging.Logger:
    """Configure and return the resume-sync logger writing JSON to stderr.

    Idempotent: re-configuring does not stack handlers. Independent of the
    Alembic logging config so CLI logs do not depend on ``alembic.ini``.
    """

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    return logger


class _JsonFormatter(logging.Formatter):
    # Reserved LogRecord attributes that must never be re-serialized when a
    # caller attaches structured ``extra`` fields. Generated once from a fresh
    # empty LogRecord-like attribute set so any field a caller adds via
    # ``log_event`` is treated as structured payload.
    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
            "message",
            "asctime",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "event": getattr(record, "event_name", record.getMessage()),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, sort_keys=True, default=str)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one structured log event with a stable ``event`` name."""

    logger.log(level, event, extra={"event_name": event, **fields})


JsonFormatter = _JsonFormatter
