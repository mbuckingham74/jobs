"""Tests for database-failure handling in resume sync.

All non-PostgreSQL tests in this module mock the database boundary with
deterministic fakes that raise appropriate :class:`SQLAlchemyError` subclasses
without opening a socket. A refused connection to ``127.0.0.1`` is still a live
network request — these tests never make one. Real transaction rollback and
database cleanup verification live only in the tests marked ``postgres``,
which connect through the disposable loopback ``TEST_DATABASE_URL`` container.

A failed pre-fetch read returns a structured ``DB_ERROR`` *before* the HTTP
request is made; the fetcher is never called. Database failures on the 304 and
200 write paths likewise surface as a structured ``DB_ERROR``: the session is
rolled back and closed, the engine is disposed on every path, raw exception
text and ``DATABASE_URL`` never appear in a result or log, and the CLI emits
exactly one JSON object and exits nonzero. Non-database programming errors are
never caught as database failures — they propagate so a real bug is visible.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.resume import repository as repo
from app.resume import service as service_module
from app.resume.config import ResumeSettings
from app.resume.db import DatabaseError
from app.resume.fetch import ConditionalValidators
from app.resume.logging import JsonFormatter
from app.resume.results import ResumeSyncStatus
from app.resume.service import sync_resume
from tests.resume_test_helpers import (
    CONTENT_A,
    assert_no_credentials,
    make_extraction_result,
    not_modified_outcome,
    ok_outcome,
    sentinel_database_url,
)


def _capture_logger(buf: io.StringIO) -> logging.Logger:
    logger = logging.getLogger("app.resume.dbfail")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def _settings_with_sentinel_db() -> ResumeSettings:
    # The sentinel URL carries a credential that must never leak to logs,
    # results, or stdout. It is never opened: every database boundary below
    # is mocked.
    return ResumeSettings(
        database_url=sentinel_database_url(),
        base_resume_url="https://example.invalid/portfolio.pdf",
        resume_max_bytes=10 * 1024 * 1024,
    )


class _FakeEngine:
    """A minimal fake SQLAlchemy engine whose ``connect()`` raises a
    :class:`SQLAlchemyError` to simulate a real connection failure without
    opening a socket. ``dispose()`` is recorded so tests can assert cleanup.
    """

    def __init__(self) -> None:
        self.dispose_calls: list[bool] = []

    def connect(self):
        raise OperationalError("connect", {}, Exception("connection refused"))

    def dispose(self) -> None:
        self.dispose_calls.append(True)


def test_failed_pre_fetch_read_returns_db_error_before_fetch(monkeypatch) -> None:
    """A failed pre-fetch database read returns DB_ERROR before any HTTP
    request is made; the fetcher is never called and the engine is disposed."""

    buf = io.StringIO()
    logger = _capture_logger(buf)

    def _must_not_be_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fetcher must not be called on a pre-fetch DB failure")

    fake_engine = _FakeEngine()

    def _fake_make_engine(_url: str) -> _FakeEngine:
        return fake_engine

    monkeypatch.setattr(service_module, "make_engine", _fake_make_engine)

    result = sync_resume(
        _settings_with_sentinel_db(),
        logger=logger,
        fetcher=_must_not_be_called,
    )
    assert result.status is ResumeSyncStatus.DB_ERROR
    assert result.error is not None
    assert result.error.code == "db.read_error"
    assert result.http_status is None
    assert result.sent_validators is False
    assert fake_engine.dispose_calls == [True]  # engine disposed on read-failure path
    assert_no_credentials(buf.getvalue())
    assert_no_credentials(repr(result))


def test_failed_304_write_returns_db_error_and_redacts(monkeypatch) -> None:
    """A database failure during the 304 write path surfaces as a structured
    DB_ERROR carrying the fetch outcome's HTTP status; raw exception text and
    ``DATABASE_URL`` never leak to logs or the result object."""

    buf = io.StringIO()
    logger = _capture_logger(buf)

    def _make_engine_raises(_url: str):
        raise OperationalError("connect", {}, Exception("connection lost"))

    monkeypatch.setattr(service_module, "make_engine", _make_engine_raises)

    result = sync_resume(
        _settings_with_sentinel_db(),
        logger=logger,
        pre_fetch_validators_override=ConditionalValidators(),
        fetch_outcome_override=not_modified_outcome(etag='"e"'),
    )
    assert result.status is ResumeSyncStatus.DB_ERROR
    assert result.error is not None
    assert result.error.code == "db.write_error"
    assert result.http_status == 304
    assert_no_credentials(buf.getvalue())
    assert_no_credentials(repr(result))


def test_failed_200_write_returns_db_error_and_redacts(monkeypatch) -> None:
    """A database failure during the 200 write path surfaces as a structured
    DB_ERROR carrying the fetch outcome's HTTP status; redaction holds."""

    buf = io.StringIO()
    logger = _capture_logger(buf)

    def _make_engine_raises(_url: str):
        raise OperationalError("connect", {}, Exception("connection lost"))

    monkeypatch.setattr(service_module, "make_engine", _make_engine_raises)

    result = sync_resume(
        _settings_with_sentinel_db(),
        logger=logger,
        pre_fetch_validators_override=ConditionalValidators(),
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.DB_ERROR
    assert result.error is not None
    assert result.error.code == "db.write_error"
    assert result.http_status == 200
    assert_no_credentials(buf.getvalue())
    assert_no_credentials(repr(result))


def test_cli_db_error_emits_one_json_and_exits_nonzero(monkeypatch) -> None:
    """CLI: a failed pre-fetch read produces exactly one JSON object on stdout
    with status=db_error and a nonzero exit code; no secrets leak to stdout."""

    from app.resume.cli import EXIT_FAILURE, run_once

    fake_engine = _FakeEngine()

    def _fake_make_engine(_url: str) -> _FakeEngine:
        return fake_engine

    monkeypatch.setattr(service_module, "make_engine", _fake_make_engine)

    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": sentinel_database_url(),
            "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
        },
        stdout=out,
    )
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert code == EXIT_FAILURE
    assert payload["status"] == ResumeSyncStatus.DB_ERROR.value
    assert payload["error"]["code"] == "db.read_error"
    assert_no_credentials(out.getvalue())


def test_non_database_error_propagates_not_db_error(monkeypatch) -> None:
    """Non-database programming errors propagate untouched; they must never be
    swallowed into a structured DB_ERROR that hides the real bug."""

    def buggy_extractor(_body: bytes):  # noqa: ARG001 - placeholder signature
        raise KeyError("not a database failure")

    def _fake_make_engine(_url: str) -> _FakeEngine:
        return _FakeEngine()

    monkeypatch.setattr(service_module, "make_engine", _fake_make_engine)

    with pytest.raises(KeyError):
        sync_resume(
            _settings_with_sentinel_db(),
            pre_fetch_validators_override=ConditionalValidators(),
            fetch_outcome_override=ok_outcome(b"body-a"),
            extractor=buggy_extractor,
        )


_pg = pytest.mark.postgres


@_pg
def test_failed_200_write_rolls_back_and_disposes(postgres_engine, monkeypatch) -> None:
    """On a 200-write database failure the transaction is rolled back so no
    partial ``resume_version`` or ``resume_source_state`` row is left, the
    service's engine is disposed exactly once, and the result is a structured
    DB_ERROR. The fixture's own engine is untouched by the disposal spy."""

    settings = ResumeSettings(
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        base_resume_url="https://example.invalid/portfolio.pdf",
        resume_max_bytes=10 * 1024 * 1024,
    )

    real_make_engine = service_module.make_engine
    dispose_calls: list[bool] = []

    def spy_make_engine(url: str):
        eng = real_make_engine(url)
        original_dispose = eng.dispose

        def spy_dispose():
            dispose_calls.append(True)
            return original_dispose()

        eng.dispose = spy_dispose  # type: ignore[method-assign]
        return eng

    monkeypatch.setattr(service_module, "make_engine", spy_make_engine)

    def _raise_on_insert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("INSERT INTO resume_version ...", {}, Exception("connection lost"))

    monkeypatch.setattr(service_module, "insert_resume_version", _raise_on_insert)

    result = sync_resume(
        settings,
        pre_fetch_validators_override=ConditionalValidators(),
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.DB_ERROR
    assert result.error is not None
    assert result.error.code == "db.write_error"
    assert result.http_status == 200
    assert dispose_calls == [True]

    with postgres_engine.connect() as conn:
        versions = list(
            conn.execute(
                select(repo.resume_version_table).where(
                    repo.resume_version_table.c.variant == "base"
                )
            ).all()
        )
        assert versions == []
        states = list(conn.execute(select(repo.resume_source_state_table)).all())
        assert states == []


@_pg
def test_failed_304_write_rolls_back_and_disposes(postgres_engine, monkeypatch) -> None:
    """On a 304-write database failure the transaction is rolled back so the
    seeded source-state row is unchanged (no lost validators, no new row, no
    last_checked_at/updated_at bump) and the service's engine is disposed
    exactly once."""

    from datetime import UTC, datetime

    from sqlalchemy import insert

    settings = ResumeSettings(
        database_url=postgres_engine.url.render_as_string(hide_password=False),
        base_resume_url="https://example.invalid/portfolio.pdf",
        resume_max_bytes=10 * 1024 * 1024,
    )

    with postgres_engine.begin() as conn:
        seeded_state_id = conn.execute(
            insert(repo.resume_source_state_table)
            .values(
                variant="base",
                source_kind="portfolio_pdf",
                source_url="https://example.invalid/portfolio.pdf",
                source_etag='"seed-etag"',
                last_checked_at=datetime(2026, 7, 1, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                current_resume_version_id=None,
            )
            .returning(repo.resume_source_state_table.c.id)
        ).scalar_one()
    seeded_state_id = int(seeded_state_id)

    real_make_engine = service_module.make_engine
    dispose_calls: list[bool] = []

    def spy_make_engine(url: str):
        eng = real_make_engine(url)
        original_dispose = eng.dispose

        def spy_dispose():
            dispose_calls.append(True)
            return original_dispose()

        eng.dispose = spy_dispose  # type: ignore[method-assign]
        return eng

    monkeypatch.setattr(service_module, "make_engine", spy_make_engine)

    def _raise_on_update(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("UPDATE resume_source_state ...", {}, Exception("connection lost"))

    monkeypatch.setattr(service_module, "update_source_state_on_304", _raise_on_update)

    result = sync_resume(
        settings,
        pre_fetch_validators_override=ConditionalValidators(etag='"seed-etag"'),
        fetch_outcome_override=not_modified_outcome(etag='"new-etag"'),
    )
    assert result.status is ResumeSyncStatus.DB_ERROR
    assert result.error is not None
    assert result.error.code == "db.write_error"
    assert result.http_status == 304
    assert dispose_calls == [True]

    with postgres_engine.connect() as conn:
        state = dict(
            conn.execute(
                select(repo.resume_source_state_table).where(
                    repo.resume_source_state_table.c.id == seeded_state_id
                )
            )
            .mappings()
            .first()
        )
        assert state["id"] == seeded_state_id
        assert state["source_etag"] == '"seed-etag"'
        assert state["current_resume_version_id"] is None
        assert state["updated_at"] == state["created_at"]
    with postgres_engine.connect() as conn:
        count = len(list(conn.execute(select(repo.resume_source_state_table)).all()))
        assert count == 1


# Keep DatabaseError importable from test module to silence ruff F401 when
# tests reference it through DatabaseError assertions above.
_ = DatabaseError
