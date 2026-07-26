"""Runtime DATABASE_URL resolution and redaction for the Alembic environment.

The migration environment reads the database URL from ``DATABASE_URL`` at
runtime. This module centralises the safe resolution, masking, and
connection-error handling so the unmasked connection string is never printed
and never round-tripped through Alembic's ConfigParser.

These helpers are kept independent of the Alembic runtime context so they can
be unit tested without invoking ``alembic``. ``migrations/env.py`` imports and
delegates to them.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

DATABASE_URL_ENV = "DATABASE_URL"

MISSING_URL_MESSAGE = (
    "DATABASE_URL is not set. Alembic reads the runtime database URL from "
    "the environment; no connection string is tracked."
)


def require_database_url(environ: dict[str, str] | None = None) -> str:
    """Return the runtime ``DATABASE_URL`` or raise ``SystemExit`` safely.

    The URL value is never part of the raised message, so a missing variable
    cannot leak a partially-populated connection string from any other env var.
    Pass an explicit ``environ`` for testing; production reads ``os.environ``.
    """

    source = environ if environ is not None else os.environ
    url = source.get(DATABASE_URL_ENV, "")
    if not url or not url.strip():
        raise SystemExit(MISSING_URL_MESSAGE)
    return url.strip()


def mask_url(url: str) -> str:
    """Return ``url`` with any password component replaced by ``***``.

    Passwords are redacted verbatim — whether plaintext or percent-encoded —
    so neither form can leak through diagnostics. Username, host, port, and
    database name are preserved because they are not secret.
    """

    parsed = urlparse(url)
    if not parsed.password:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    userinfo = parsed.username or ""
    netloc = f"{userinfo}:***@{host}" if userinfo else f"***@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


def connect_error_message(url: str) -> str:
    """Build the safe message emitted when the database cannot be reached."""

    return (
        "Could not connect to the database for Alembic "
        f"({mask_url(url)}). The connection string is redacted; verify "
        "DATABASE_URL and database availability."
    )


def connect_or_exit(
    url: str,
    poolclass: type = pool.NullPool,
    connect_args: dict[str, object] | None = None,
) -> tuple[Engine, object]:
    """Create the engine and open a connection for online migrations.

    The URL is passed directly to ``create_engine`` and never written to the
    Alembic config. Any ``SQLAlchemyError`` raised while building the engine or
    opening the connection is converted to ``SystemExit`` carrying a redacted
    message and no chained traceback, so the unmasked string cannot escape
    through the SQLAlchemy error or its stack trace.

    Returns ``(engine, connection)`` on success. The caller owns closing both.
    """

    try:
        engine = create_engine(url, poolclass=poolclass, connect_args=connect_args or {})
        connection = engine.connect()
    except SQLAlchemyError:
        raise SystemExit(connect_error_message(url)) from None
    return engine, connection
