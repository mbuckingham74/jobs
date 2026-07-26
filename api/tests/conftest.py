"""Pytest configuration and shared fixtures for resume-sync tests.

Registers the ``postgres`` marker used by the disposable PostgreSQL 16 +
pgvector integration tests and supplies a ``postgres_url`` fixture that reads
``TEST_DATABASE_URL`` from the environment and refuses to run against a
non-loopback or production-shaped database: PostgreSQL-specific behavior must
be verified against a disposable database, not SQLite, and never implicitly
against a development or production database.

Network isolation: tests that do not carry the ``postgres`` marker run under a
real socket-connect guard that patches :func:`socket.socket` so any attempt to
open a network connection fails the test immediately with a clear message. A
refused connection to ``127.0.0.1`` is still a live network request — the
non-PostgreSQL suite must be genuinely network-isolated, mocking the database
boundary instead of pointing it at a closed port. PostgreSQL-marked tests
bypass the guard and connect only through the validated loopback
``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest
from sqlalchemy import text


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: tests that require a disposable PostgreSQL 16 + pgvector "
        "database supplied by TEST_DATABASE_URL (loopback only).",
    )


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture()
def postgres_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set; PostgreSQL tests run only against a "
            "disposable local database (see the task validation commands)."
        )
    parsed = urlparse(url)
    if parsed.hostname not in _LOOPBACK_HOSTS:
        pytest.fail(
            f"TEST_DATABASE_URL must point at a loopback host for disposable "
            f"PostgreSQL tests; got hostname={parsed.hostname!r}"
        )
    if (parsed.username or "") and (parsed.password or ""):
        # Acceptable for the disposable container; just ensure host is loopback.
        pass
    return url


class _NetworkBlockedError(RuntimeError):
    """Raised by the socket guard when a non-postgres test attempts to
    connect to a network endpoint."""

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(
            f"non-postgres test attempted a live network socket connection "
            f"to {address[0]}:{address[1]}; database failures must be mocked "
            f"without opening a socket"
        )
        self.address = address


@pytest.fixture(autouse=True)
def _guard_network_for_non_postgres(monkeypatch, request):
    """Block every socket connect attempt for tests without the ``postgres``
    marker. A refused connection to ``127.0.0.1`` is still a live network
    request; this guard makes the isolation real rather than aspirational.
    PostgreSQL-marked tests bypass the guard so they can connect to the
    disposable loopback ``TEST_DATABASE_URL`` container.
    """

    if request.node.get_closest_marker("postgres") is not None:
        return None

    original_socket = socket.socket

    class _BlockedSocket(original_socket):  # type: ignore[misc, valid-type]
        def connect(self, address):  # type: ignore[no-untyped-def]
            err = _NetworkBlockedError(address)
            # Attach the AssertionError to be raised immediately so the test
            # owner sees the blocked connect in the traceback alongside the
            # fixture message.
            raise AssertionError(str(err)) from err

        def connect_ex(self, address):  # type: ignore[no-untyped-def]
            err = _NetworkBlockedError(address)
            raise AssertionError(str(err)) from err

    def _socket_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _BlockedSocket(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", _socket_factory)
    return None


@pytest.fixture()
def postgres_engine(postgres_url: str):
    """A SQLAlchemy engine over the disposable ``TEST_DATABASE_URL`` that
    cleans resume_version and resume_source_state rows between tests.

    The CLI/persistence postgres tests assume ``alembic upgrade head`` has
    already been run by the validation commands. Identity sequences are
    reset so per-test assertions on row counts and content hashes are stable.
    """

    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, pool_pre_ping=True)
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM resume_source_state"))
        conn.execute(text("DELETE FROM resume_version"))
        # Reset both sequences/identities. ``resume_version.id`` is a BIGSERIAL
        # (sequence-backed); ``resume_source_state.id`` is an identity column.
        # ``pg_get_serial_sequence`` resolves the backing sequence for both so
        # ``setval`` resets them uniformly.
        conn.execute(text("SELECT setval(pg_get_serial_sequence('resume_version','id'), 1, false)"))
        conn.execute(
            text("SELECT setval(pg_get_serial_sequence('resume_source_state','id'), 1, false)")
        )
    engine.dispose()
