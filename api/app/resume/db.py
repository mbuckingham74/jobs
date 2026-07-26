"""SQLAlchemy engine and session factory for resume sync.

A narrowly scoped engine/session with explicit transaction ownership and safe
connection-error handling. The application startup path does not import this
module, so the FastAPI health check stays independent of PostgreSQL. The CLI
is the only caller that constructs an engine, and it disposes the engine on
every path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker


class DatabaseError(Exception):
    """Raised when the resume-sync database cannot be reached or used.

    Carries only a safe reason code, never the connection string or any
    chained exception string that might echo a password or query parameter.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def make_engine(database_url: str) -> Engine:
    try:
        return create_engine(database_url, pool_pre_ping=True)
    except SQLAlchemyError:
        raise DatabaseError("db.invalid_url") from None


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Open one short SQLAlchemy session and close it on every path.

    The caller owns transaction boundaries (``begin()``/``commit()``) inside
    the advisory-lock critical section so the lock is held for the shortest
    possible interval. A failed session is closed without raising a raw
    SQLAlchemy exception: the caller receives a :class:`DatabaseError` with a
    safe reason code instead.
    """

    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
    except SQLAlchemyError:
        session.rollback()
        raise DatabaseError("db.error") from None
    finally:
        session.close()
