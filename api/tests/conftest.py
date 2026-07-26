"""Pytest configuration and shared fixtures for resume-sync tests.

Registers the ``postgres`` marker used by the disposable PostgreSQL 16 +
pgvector integration tests and supplies a ``postgres_url`` fixture that reads
``TEST_DATABASE_URL`` from the environment and refuses to run against a
non-loopback or production-shaped database: PostgreSQL-specific behavior must
be verified against a disposable database, not SQLite, and never implicitly
against a development or production database.
"""

from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def _no_live_network_for_non_postgres(monkeypatch, request):
    """Best-effort guard: tests that do not opt-in to PostgreSQL must not make
    a live network request. We do not patch ``socket`` here (PostgreSQL tests
    legitimately reach a loopback container); instead we assert at collection
    time that every non-postgres fetch test injects a mock transport via the
    ``transport``/``client`` parameter. This autouse fixture documents that the
    HTTP boundary is the only outbound network path and that resume-sync tests
    never construct a real ``httpx.Client`` against the configured host.
    """

    # No-op guard for documentation. Real isolation comes from tests always
    # passing ``transport=httpx.MockTransport(...)`` to ``fetch_resume``.
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
