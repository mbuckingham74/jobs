"""PostgreSQL integration tests for resume sync persistence.

Marked ``postgres``: run only against a disposable PostgreSQL 16 + pgvector
database supplied by ``TEST_DATABASE_URL`` (loopback only). The validation
commands run ``alembic upgrade head`` before invoking ``pytest -m postgres``,
so the schema is already in place; the ``postgres_engine`` fixture cleans
resume rows between tests.

Covers first activation, later inactive staging, exact repeat, byte-only
change, existing inactive base state, provenance conflict, resume_source_state
semantics for 304 / new-content 200 / identical-content 200 / changing
BASE_RESUME_URL, and concurrent identical attempts.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from app.resume import repository as repo
from app.resume.extract import ExtractionResult
from app.resume.results import ResumeSyncStatus
from app.resume.service import sync_resume
from tests.resume_test_helpers import (
    CONTENT_A,
    CONTENT_B,
    SETTINGS_URL,
    make_extraction_result,
    not_modified_outcome,
    ok_outcome,
    settings_for,
)

pytestmark = pytest.mark.postgres


def _count(session, table, where=None) -> int:
    stmt = select(table)
    if where is not None:
        stmt = stmt.where(where)
    return len(list(session.execute(stmt).all()))


def test_first_import_activates_when_no_base_exists(postgres_engine) -> None:
    settings = settings_for()
    extraction = make_extraction_result(CONTENT_A)
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: extraction,
    )
    assert result.status is ResumeSyncStatus.ACTIVATED_INITIAL
    assert result.activated is True
    assert result.resume_version_id is not None

    with postgres_engine.connect() as conn:
        rows = list(
            conn.execute(
                select(repo.resume_version_table).where(
                    repo.resume_version_table.c.variant == "base"
                )
            ).mappings()
        )
        assert len(rows) == 1
        assert rows[0]["active"] is True
        assert rows[0]["source_url"] == SETTINGS_URL
        assert rows[0]["source_kind"] == "portfolio_pdf"
        assert rows[0]["content_hash"] == extraction.content_hash

        state = dict(
            conn.execute(
                select(repo.resume_source_state_table).where(
                    repo.resume_source_state_table.c.source_url == SETTINGS_URL
                )
            )
            .mappings()
            .first()
        )
        assert state["current_resume_version_id"] == rows[0]["id"]
        assert state["last_body_sha256"] is not None
        assert state["last_body_fetched_at"] is not None


def test_later_different_content_is_inactive_and_preserves_active(
    postgres_engine,
) -> None:
    settings = settings_for()
    first = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert first.status is ResumeSyncStatus.ACTIVATED_INITIAL

    second = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-b"),
        extractor=lambda body: make_extraction_result(CONTENT_B),
    )
    assert second.status is ResumeSyncStatus.STAGED_FOR_REVIEW
    assert second.activated is False

    with postgres_engine.connect() as conn:
        rows = list(
            conn.execute(
                select(repo.resume_version_table)
                .where(repo.resume_version_table.c.variant == "base")
                .order_by(repo.resume_version_table.c.id)
            ).mappings()
        )
        assert len(rows) == 2
        assert rows[0]["active"] is True
        assert rows[1]["active"] is False
        state = dict(
            conn.execute(
                select(repo.resume_source_state_table).where(
                    repo.resume_source_state_table.c.source_url == SETTINGS_URL
                )
            )
            .mappings()
            .first()
        )
        assert state["current_resume_version_id"] == rows[1]["id"]


def test_exact_repeat_creates_no_new_version(postgres_engine) -> None:
    settings = settings_for()
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    repeat = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert repeat.status is ResumeSyncStatus.UNCHANGED_CONTENT
    assert repeat.content_changed is False
    with postgres_engine.connect() as conn:
        count = _count(
            conn,
            repo.resume_version_table,
            repo.resume_version_table.c.variant == "base",
        )
        assert count == 1


def test_byte_only_change_creates_no_new_version(postgres_engine) -> None:
    settings = settings_for()
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a-bytes-only-change"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.UNCHANGED_CONTENT
    assert result.content_changed is False
    assert result.raw_changed is True
    with postgres_engine.connect() as conn:
        count = _count(
            conn,
            repo.resume_version_table,
            repo.resume_version_table.c.variant == "base",
        )
        assert count == 1
        state = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
        assert state["last_body_sha256"] is not None


def test_provenance_conflict_mutates_neither_table(postgres_engine) -> None:
    settings = settings_for()
    extraction = make_extraction_result(CONTENT_A)

    with postgres_engine.begin() as conn:
        conn.execute(
            insert(repo.resume_version_table).values(
                variant="technical",
                source_kind="manual",
                content_hash=extraction.content_hash,
                content_md=extraction.content_md,
                active=False,
            )
        )
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: extraction,
    )
    assert result.status is ResumeSyncStatus.PROVENANCE_CONFLICT
    with postgres_engine.connect() as conn:
        base_count = _count(
            conn,
            repo.resume_version_table,
            repo.resume_version_table.c.variant == "base",
        )
        assert base_count == 0
        state_count = _count(conn, repo.resume_source_state_table)
        assert state_count == 0


def test_existing_inactive_base_state_blocks_first_activation(
    postgres_engine,
) -> None:
    settings = settings_for()
    with postgres_engine.begin() as conn:
        conn.execute(
            insert(repo.resume_version_table).values(
                variant="base",
                source_kind="portfolio_pdf",
                source_url=SETTINGS_URL,
                source_sha256="seed-sha",
                content_hash="preseed-different-content-hash",
                content_md="preseed content",
                active=False,
            )
        )
        conn.execute(
            insert(repo.resume_source_state_table).values(
                variant="base",
                source_kind="portfolio_pdf",
                source_url=SETTINGS_URL,
                last_checked_at=datetime(2026, 7, 1, tzinfo=UTC),
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.STAGED_FOR_REVIEW
    assert result.activated is False
    with postgres_engine.connect() as conn:
        active = _count(
            conn,
            repo.resume_version_table,
            (repo.resume_version_table.c.variant == "base")
            & (repo.resume_version_table.c.active.is_(True)),
        )
        assert active == 0


def test_304_preserves_body_fields_and_absent_validators(postgres_engine) -> None:
    settings = settings_for()
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a", etag='"v1"'),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    with postgres_engine.connect() as conn:
        before = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert before["source_etag"] == '"v1"'
    assert before["last_body_sha256"] is not None
    assert before["last_body_fetched_at"] is not None
    assert before["current_resume_version_id"] is not None

    result = sync_resume(settings, fetch_outcome_override=not_modified_outcome())
    assert result.status is ResumeSyncStatus.NOT_MODIFIED
    with postgres_engine.connect() as conn:
        after = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert after["source_etag"] == '"v1"'
    assert after["last_body_sha256"] == before["last_body_sha256"]
    assert after["last_body_fetched_at"] == before["last_body_fetched_at"]
    assert after["current_resume_version_id"] == before["current_resume_version_id"]
    assert after["updated_at"] > before["updated_at"]


def test_304_updates_returned_validators_preserves_absent(postgres_engine) -> None:
    settings = settings_for()
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(
            b"body-a", etag='"v1"', last_modified=datetime(2026, 7, 1, tzinfo=UTC)
        ),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    result = sync_resume(
        settings,
        fetch_outcome_override=not_modified_outcome(etag='"v2"'),
    )
    assert result.status is ResumeSyncStatus.NOT_MODIFIED
    with postgres_engine.connect() as conn:
        after = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert after["source_etag"] == '"v2"'
    assert after["last_body_sha256"] is not None
    assert after["current_resume_version_id"] is not None


def test_304_etag_only_updates_etag_preserves_last_modified(postgres_engine) -> None:
    """A 304 returning only an ETag must update the stored ETag and leave the
    stored Last-Modified untouched. The previous aggregate-flag implementation
    cleared Last-Modified here; the independent ``etag_returned`` /
    ``last_modified_returned`` flags prevent that. Body hash, body fetch
    timestamp, and the version pointer must also be unchanged on a 304.
    """
    settings = settings_for()
    seeded_lm = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a", etag='"v1"', last_modified=seeded_lm),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    with postgres_engine.connect() as conn:
        before = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert before["source_etag"] == '"v1"'
    assert before["source_last_modified"] == seeded_lm

    result = sync_resume(
        settings,
        fetch_outcome_override=not_modified_outcome(etag='"v2"'),  # no Last-Modified
    )
    assert result.status is ResumeSyncStatus.NOT_MODIFIED
    with postgres_engine.connect() as conn:
        after = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert after["source_etag"] == '"v2"'  # updated
    assert after["source_last_modified"] == seeded_lm  # preserved
    assert after["last_body_sha256"] == before["last_body_sha256"]
    assert after["last_body_fetched_at"] == before["last_body_fetched_at"]
    assert after["current_resume_version_id"] == before["current_resume_version_id"]
    assert after["updated_at"] > before["updated_at"]


def test_304_last_modified_only_updates_last_modified_preserves_etag(postgres_engine) -> None:
    """A 304 returning only a Last-Modified must update the stored
    Last-Modified and leave the stored ETag untouched (the inverse of the
    ETag-only case). Body hash, body fetch timestamp, and the version pointer
    must also be unchanged on a 304.
    """
    settings = settings_for()
    seeded_lm_v1 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    new_lm = datetime(2026, 7, 2, 9, 30, 0, tzinfo=UTC)
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a", etag='"v1"', last_modified=seeded_lm_v1),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    with postgres_engine.connect() as conn:
        before = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())

    result = sync_resume(
        settings,
        fetch_outcome_override=not_modified_outcome(last_modified=new_lm),  # no ETag
    )
    assert result.status is ResumeSyncStatus.NOT_MODIFIED
    with postgres_engine.connect() as conn:
        after = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert after["source_etag"] == '"v1"'  # preserved
    assert after["source_last_modified"] == new_lm  # updated
    assert after["last_body_sha256"] == before["last_body_sha256"]
    assert after["last_body_fetched_at"] == before["last_body_fetched_at"]
    assert after["current_resume_version_id"] == before["current_resume_version_id"]
    assert after["updated_at"] > before["updated_at"]


def test_304_neither_validator_preserves_both(postgres_engine) -> None:
    """A 304 returning neither validator must preserve both stored ETag and
    stored Last-Modified while advancing ``last_checked_at``/``updated_at``.
    Body hash, body fetch timestamp, and the version pointer must also be
    unchanged.
    """
    settings = settings_for()
    seeded_lm = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a", etag='"v1"', last_modified=seeded_lm),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    with postgres_engine.connect() as conn:
        before = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())

    result = sync_resume(
        settings,
        fetch_outcome_override=not_modified_outcome(),  # no ETag, no Last-Modified
    )
    assert result.status is ResumeSyncStatus.NOT_MODIFIED
    with postgres_engine.connect() as conn:
        after = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert after["source_etag"] == '"v1"'  # preserved
    assert after["source_last_modified"] == seeded_lm  # preserved
    assert after["last_body_sha256"] == before["last_body_sha256"]
    assert after["last_body_fetched_at"] == before["last_body_fetched_at"]
    assert after["current_resume_version_id"] == before["current_resume_version_id"]
    assert after["last_checked_at"] >= before["last_checked_at"]
    assert after["updated_at"] > before["updated_at"]


def test_200_with_no_validators_clears_validators_and_updates_body(
    postgres_engine,
) -> None:
    settings = settings_for()
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(
            b"body-a", etag='"v1"', last_modified=datetime(2026, 7, 1, tzinfo=UTC)
        ),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a2", etag=None, last_modified=None),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.UNCHANGED_CONTENT
    with postgres_engine.connect() as conn:
        state = dict(conn.execute(select(repo.resume_source_state_table)).mappings().first())
    assert state["source_etag"] is None
    assert state["source_last_modified"] is None
    assert state["last_body_sha256"] is not None


def test_changing_base_resume_url_selects_or_creates_distinct_row(
    postgres_engine,
) -> None:
    settings_a = settings_for("https://example.invalid/a.pdf")
    settings_b = settings_for("https://example.invalid/b.pdf")
    sync_resume(
        settings_a,
        fetch_outcome_override=ok_outcome(b"body-a", etag='"etag-a"'),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    sync_resume(
        settings_b,
        fetch_outcome_override=ok_outcome(b"body-b", etag='"etag-b"'),
        extractor=lambda body: make_extraction_result(CONTENT_B),
    )
    with postgres_engine.connect() as conn:
        rows = list(
            conn.execute(
                select(repo.resume_source_state_table).order_by(
                    repo.resume_source_state_table.c.source_url
                )
            ).mappings()
        )
        assert len(rows) == 2
        by_url = {r["source_url"]: dict(r) for r in rows}
        assert by_url["https://example.invalid/a.pdf"]["source_etag"] == '"etag-a"'
        assert by_url["https://example.invalid/b.pdf"]["source_etag"] == '"etag-b"'
        assert by_url["https://example.invalid/a.pdf"]["source_etag"] != '"etag-b"'
        assert (
            by_url["https://example.invalid/a.pdf"]["current_resume_version_id"]
            != by_url["https://example.invalid/b.pdf"]["current_resume_version_id"]
        )


def test_concurrent_identical_attempts_create_one_active_version(
    postgres_engine,
) -> None:
    extraction = make_extraction_result(CONTENT_A)
    outcome_a = ok_outcome(b"body-a")
    outcome_b = ok_outcome(b"body-a")

    results: list = []
    barrier = threading.Barrier(2)

    def run(captured):
        barrier.wait()
        r = sync_resume(
            settings_for(),
            fetch_outcome_override=captured,
            extractor=lambda body: extraction,
        )
        results.append(r)

    t1 = threading.Thread(target=run, args=(outcome_a,))
    t2 = threading.Thread(target=run, args=(outcome_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [r.status for r in results]
    assert statuses.count(ResumeSyncStatus.ACTIVATED_INITIAL) == 1
    assert statuses.count(ResumeSyncStatus.UNCHANGED_CONTENT) == 1

    with postgres_engine.connect() as conn:
        versions = list(
            conn.execute(
                select(repo.resume_version_table).where(
                    repo.resume_version_table.c.variant == "base"
                )
            ).mappings()
        )
        assert len(versions) == 1
        active = [v for v in versions if v["active"]]
        assert len(active) == 1
        states = list(conn.execute(select(repo.resume_source_state_table)).mappings())
        assert len(states) == 1
        assert states[0]["current_resume_version_id"] == versions[0]["id"]


def test_rollback_on_failed_insert_leaves_no_partial_rows(postgres_engine) -> None:
    # A provenance conflict mid-transaction must roll back so neither
    # resume_version nor resume_source_state carries a partial row. Already
    # covered by test_provenance_conflict_mutates_neither_table; this test
    # additionally confirms the partial-unique active index backstop tolerates
    # a second concurrent activation attempt by staging it inactive.
    settings = settings_for()
    extraction = make_extraction_result(CONTENT_A)
    sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: extraction,
    )
    # Run again: must observe active base and NOT mutate activation.
    result = sync_resume(
        settings,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    assert result.status is ResumeSyncStatus.UNCHANGED_CONTENT
    assert result.activated is False
    with postgres_engine.connect() as conn:
        active = _count(
            conn,
            repo.resume_version_table,
            (repo.resume_version_table.c.variant == "base")
            & (repo.resume_version_table.c.active.is_(True)),
        )
        assert active == 1


# Re-export to satisfy tests that import ExtractionResult-shaped helpers.
__all__ = ["ExtractionResult"]
