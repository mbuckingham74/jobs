"""Public-path PostgreSQL integration checks for Task 008 atomic persistence.

These tests exercise ``evaluate_hard_filters(...)`` end to end: evaluation,
atomic H/S persistence, replay, provenance, race recovery, and the closed
error surface. The repository owns the transaction; the service owns the
snapshot, evaluation, and canonical construction.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text

import app.filters.repository as filter_repository
import app.filters.service as filter_service
from app.filters import HardFilterReason, evaluate_hard_filters
from app.filters.results import (
    FilterDatabaseError,
    FilterErrorCode,
    FilterEvaluationConflictError,
    FilterEvaluatorDefectError,
    FilterPersistedInputError,
    PostingNotFoundError,
    StaleFilterInputError,
)

pytestmark = pytest.mark.postgres

BASE = datetime(2026, 8, 1, 12, tzinfo=UTC)
COMPANY_ID = 880001
ENDPOINT_ID = 880002
RUN_ID = 880003
RUN_ID_2 = 880013
POSTING_ID = 880004
VERSION_ID = 880005
ACTION_SCORE_ID = 880006
SCORE_ID_2 = 880014
DIGEST_ID = 880007
DIGEST_ITEM_ID = 880008

_UNRELATED_SCORE_COLUMNS = (
    "resume_version_id",
    "candidate_profile_version_id",
    "company_research_id",
    "model_id",
    "prompt_version",
    "rubric_version",
    "overall",
    "domain_fit",
    "seniority_fit",
    "remote_fit",
    "company_fit",
    "axis_notes",
    "verdict",
    "location_eligibility",
    "rationale",
    "gaps",
    "hooks",
    "red_flags",
    "resume_variant",
)


def _cleanup(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM digest_item WHERE id = :id"), {"id": DIGEST_ITEM_ID})
        conn.execute(text("DELETE FROM daily_digest WHERE id = :id"), {"id": DIGEST_ID})
        conn.execute(
            text("DELETE FROM hard_filter_evaluation WHERE posting_id = :id"), {"id": POSTING_ID}
        )
        conn.execute(text("DELETE FROM score WHERE posting_version_id = :id"), {"id": VERSION_ID})
        conn.execute(text("DELETE FROM posting_version WHERE id = :id"), {"id": VERSION_ID})
        conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        conn.execute(text("DELETE FROM source_endpoint WHERE id = :id"), {"id": ENDPOINT_ID})
        conn.execute(text("DELETE FROM pipeline_run WHERE id = :id"), {"id": RUN_ID})
        conn.execute(text("DELETE FROM pipeline_run WHERE id = :id"), {"id": RUN_ID_2})
        conn.execute(text("DELETE FROM company WHERE id = :id"), {"id": COMPANY_ID})


def _seed(
    engine,
    *,
    title: str,
    description: str | None,
    locations=(),
    source_published_at=None,
    first_seen_at=BASE,
    blocked: bool = False,
    closed_at=None,
) -> None:
    _cleanup(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO company (id, name, blocked) "
                "VALUES (:id, 'Task008 Invented Company', :blocked)"
            ),
            {"id": COMPANY_ID, "blocked": blocked},
        )
        conn.execute(
            text(
                "INSERT INTO source_endpoint (id, company_id, kind, token) "
                "VALUES (:id, :company_id, 'custom', 'task008-invented')"
            ),
            {"id": ENDPOINT_ID, "company_id": COMPANY_ID},
        )
        conn.execute(
            text(
                "INSERT INTO pipeline_run (id, run_kind, status, config_version, started_at) "
                "VALUES (:id, 'daily', 'running', 'task008-test', :started_at)"
            ),
            {"id": RUN_ID, "started_at": BASE},
        )
        conn.execute(
            text(
                "INSERT INTO posting (id, company_id, source_endpoint_id, external_id, title, "
                "title_norm, "
                "locations, posting_url, apply_url, source_published_at, first_seen_at, closed_at) "
                "VALUES (:id, :company_id, :endpoint_id, 'task008-job', :title, :title_norm, "
                "'[]'::jsonb, 'https://example.invalid/job', 'https://example.invalid/apply', "
                ":source_published_at, :first_seen, :closed_at)"
            ),
            {
                "id": POSTING_ID,
                "company_id": COMPANY_ID,
                "endpoint_id": ENDPOINT_ID,
                "title": title,
                "title_norm": title.casefold(),
                "source_published_at": source_published_at,
                "first_seen": first_seen_at,
                "closed_at": closed_at,
            },
        )
        conn.execute(
            text(
                "INSERT INTO posting_version (id, posting_id, observed_in_run_id, content_hash, "
                "title, locations, description_md, raw_payload) VALUES "
                "(:id, :posting_id, :run_id, :content_hash, :title, CAST(:locations AS jsonb), "
                ":description, '{}'::jsonb)"
            ),
            {
                "id": VERSION_ID,
                "posting_id": POSTING_ID,
                "run_id": RUN_ID,
                "content_hash": "b" * 64,
                "title": title,
                "description": description,
                "locations": json.dumps(list(locations)),
            },
        )
        conn.execute(
            text("UPDATE posting SET current_version_id = :version_id WHERE id = :posting_id"),
            {"version_id": VERSION_ID, "posting_id": POSTING_ID},
        )


def _evaluate(engine):
    return evaluate_hard_filters(
        engine=engine,
        posting_id=POSTING_ID,
        policy_version="phase1-hard-filters-v1",
        evaluation_as_of=BASE,
    )


def _insert_digest_action(engine, *, state: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO score (id, run_id, posting_version_id, stage, input_hash, verdict) "
                "VALUES (:id, :run_id, :version_id, 'triage', :input_hash, NULL)"
            ),
            {
                "id": ACTION_SCORE_ID,
                "run_id": RUN_ID,
                "version_id": VERSION_ID,
                "input_hash": "c" * 64,
            },
        )
        conn.execute(
            text(
                "INSERT INTO daily_digest (id, digest_on, run_id) "
                "VALUES (:id, :digest_on, :run_id)"
            ),
            {"id": DIGEST_ID, "digest_on": BASE.date(), "run_id": RUN_ID},
        )
        conn.execute(
            text(
                "INSERT INTO digest_item (id, digest_id, posting_id, score_id, rank, state) "
                "VALUES (:id, :digest_id, :posting_id, :score_id, 1, :state)"
            ),
            {
                "id": DIGEST_ITEM_ID,
                "digest_id": DIGEST_ID,
                "posting_id": POSTING_ID,
                "score_id": ACTION_SCORE_ID,
                "state": state,
            },
        )


def test_rejected_evaluation_writes_one_filter_score_and_replays(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        result = evaluate_hard_filters(
            engine=engine,
            posting_id=POSTING_ID,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=BASE,
        )
        assert result.replayed is False
        assert result.rejection_reasons == (HardFilterReason.TITLE_NO_MATCH,)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id=:id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 1
            )
            row = (
                conn.execute(
                    text("SELECT * FROM score WHERE posting_version_id=:id"),
                    {"id": VERSION_ID},
                )
                .mappings()
                .one()
            )
            assert row["stage"] == "filter"
            assert row["ruleset_version"] == "phase1-hard-filters-v1"
            assert row["reject_reasons"] == ["title_no_match"]
            assert row["run_id"] == RUN_ID
            assert row["posting_version_id"] == VERSION_ID
            assert row["input_hash"] == result.input_hash
            assert row["scored_at"] == BASE
            assert all(row[column] is None for column in _UNRELATED_SCORE_COLUMNS)
        replay = evaluate_hard_filters(
            engine=engine,
            posting_id=POSTING_ID,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=BASE,
        )
        assert replay.replayed is True
        assert replay.evaluation_id == result.evaluation_id
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM score WHERE posting_version_id=:id"),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_eligible_evaluation_writes_no_filter_score_and_replays(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        result = evaluate_hard_filters(
            engine=engine,
            posting_id=POSTING_ID,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=BASE,
        )
        assert result.eligible is True
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM score WHERE posting_version_id=:id"),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
        replay = evaluate_hard_filters(
            engine=engine,
            posting_id=POSTING_ID,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=BASE,
        )
        assert replay.replayed is True
        assert replay.evaluation_id == result.evaluation_id
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM score WHERE posting_version_id=:id"),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_engine_resources_close_engine_remains_reusable_and_is_not_disposed(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url, pool_size=2, max_overflow=0)
    original_dispose = engine.dispose
    dispose_calls = 0

    def dispose_spy(*args, **kwargs):
        nonlocal dispose_calls
        dispose_calls += 1
        return original_dispose(*args, **kwargs)

    engine.dispose = dispose_spy  # type: ignore[method-assign]
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        _evaluate(engine)
        assert engine.pool.checkedout() == 0
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
        assert engine.pool.checkedout() == 0
        assert dispose_calls == 0
    finally:
        _cleanup(engine)
        engine.dispose = original_dispose  # type: ignore[method-assign]
        original_dispose()


def test_multi_reason_rejection_and_score_projection_use_canonical_order(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(
            engine,
            title="Analyst Product Marketing Manager",
            description="Must work from the office. Must possess an active security clearance",
            source_published_at=BASE - timedelta(days=46),
            blocked=True,
        )
        result = _evaluate(engine)
        expected = (
            HardFilterReason.TITLE_NO_MATCH,
            HardFilterReason.SENIORITY_LOW,
            HardFilterReason.WRONG_DISCIPLINE,
            HardFilterReason.NOT_REMOTE,
            HardFilterReason.CLEARANCE_REQUIRED,
            HardFilterReason.STALE,
            HardFilterReason.COMPANY_BLOCKED,
        )
        assert result.rejection_reasons == expected
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT reject_reasons FROM score WHERE posting_version_id = :id"),
                {"id": VERSION_ID},
            ).scalar_one() == [reason.value for reason in expected]
    finally:
        _cleanup(engine)
        engine.dispose()


def test_injected_failure_between_evaluation_and_score_writes_rolls_back_both(
    postgres_url: str, monkeypatch
) -> None:
    engine = create_engine(postgres_url)
    real_insert_score = filter_repository._insert_score

    def fail_score_insert(conn, record):
        real_insert_score(conn, record)
        raise RuntimeError("injected paired-write failure")

    monkeypatch.setattr(filter_repository, "_insert_score", fail_score_insert)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        with pytest.raises(FilterEvaluatorDefectError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.EVALUATOR_DEFECT
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM score WHERE posting_version_id = :id"),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_concurrent_identical_rejected_calls_converge_on_one_pair(postgres_url: str) -> None:
    engine = create_engine(postgres_url, pool_size=4, max_overflow=0)
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(_evaluate(engine))
        except Exception as exc:  # noqa: BLE001 - surfaced in assertions
            errors.append(exc)

    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert errors == []
        assert sorted(result.replayed for result in results) == [False, True]
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "mismatched"])
def test_replay_rejects_missing_corrupt_or_mismatched_score_pair(
    postgres_url: str, mutation: str
) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        result = _evaluate(engine)
        with engine.begin() as conn:
            if mutation == "missing":
                conn.execute(
                    text("DELETE FROM score WHERE posting_version_id = :id"),
                    {"id": VERSION_ID},
                )
            elif mutation == "corrupt":
                conn.execute(
                    text(
                        "UPDATE score SET reject_reasons = CAST(:reasons AS jsonb) "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"reasons": json.dumps(["seniority_low"]), "id": VERSION_ID},
                )
            else:
                conn.execute(
                    text(
                        "UPDATE score SET scored_at = :scored_at "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"scored_at": BASE.replace(hour=13), "id": VERSION_ID},
                )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.EVALUATION_CONFLICT
        assert result.replayed is False
        with engine.connect() as conn:
            remaining = conn.execute(
                text("SELECT count(*) FROM score WHERE posting_version_id = :id"),
                {"id": VERSION_ID},
            ).scalar_one()
            if mutation == "missing":
                assert remaining == 0, "a missing score must not be repaired"
            else:
                assert remaining == 1, "a corrupt score must not be replaced"
    finally:
        _cleanup(engine)
        engine.dispose()


def test_unexpected_score_for_eligible_evaluation_is_a_conflict(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        result = _evaluate(engine)
        with engine.begin() as conn:
            input_hash = conn.execute(
                text("SELECT input_hash FROM hard_filter_evaluation WHERE id = :id"),
                {"id": result.evaluation_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO score (id, run_id, posting_version_id, stage, ruleset_version, "
                    "input_hash, reject_reasons, scored_at) VALUES "
                    "(:id, :run_id, :version_id, 'filter', 'phase1-hard-filters-v1', "
                    ":input_hash, '[]'::jsonb, :scored_at)"
                ),
                {
                    "id": ACTION_SCORE_ID,
                    "run_id": RUN_ID,
                    "version_id": VERSION_ID,
                    "input_hash": input_hash,
                    "scored_at": BASE,
                },
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.EVALUATION_CONFLICT
    finally:
        _cleanup(engine)
        engine.dispose()


def test_stale_recheck_rolls_back_without_persisting_evaluation_or_score(
    postgres_url: str, monkeypatch
) -> None:
    engine = create_engine(postgres_url)
    real_evaluate = filter_service.evaluate_pure

    def mutate_between_read_and_persist(pure_input, *, evaluation_as_of):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE posting SET first_seen_at = :ts WHERE id = :pid"),
                {"ts": BASE.replace(minute=13), "pid": POSTING_ID},
            )
        return real_evaluate(pure_input, evaluation_as_of=evaluation_as_of)

    monkeypatch.setattr(filter_service, "evaluate_pure", mutate_between_read_and_persist)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        with pytest.raises(StaleFilterInputError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.INPUT_STALE
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM score WHERE posting_version_id = :id"),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def _dangling_run_reference(engine) -> None:
    """Corrupt ``posting_version.observed_in_run_id`` to name a nonexistent
    run by temporarily removing the FK, which RESTRICT would otherwise block."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE posting_version DROP CONSTRAINT IF EXISTS "
                "posting_version_observed_in_run_id_fkey"
            )
        )
        conn.execute(
            text("UPDATE posting_version SET observed_in_run_id = :rid WHERE id = :id"),
            {"rid": RUN_ID + 999, "id": VERSION_ID},
        )


def _restore_run_reference(engine) -> None:
    """Restore the FK (validating existing rows) after the dangling state."""
    with engine.begin() as conn:
        exists = conn.execute(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'posting_version_observed_in_run_id_fkey'"
            )
        ).scalar_one_or_none()
        conn.execute(
            text("UPDATE posting_version SET observed_in_run_id = :rid WHERE id = :id"),
            {"rid": RUN_ID, "id": VERSION_ID},
        )
        if exists is None:
            conn.execute(
                text(
                    "ALTER TABLE posting_version ADD CONSTRAINT "
                    "posting_version_observed_in_run_id_fkey "
                    "FOREIGN KEY (observed_in_run_id) REFERENCES pipeline_run (id) "
                    "ON DELETE RESTRICT"
                )
            )


def test_stale_observed_run_id_is_input_stale(postgres_url: str, monkeypatch) -> None:
    engine = create_engine(postgres_url)
    real_evaluate = filter_service.evaluate_pure

    def mutate_run_between_read_and_persist(pure_input, *, evaluation_as_of):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE posting_version SET observed_in_run_id = :rid WHERE id = :vid"),
                {"rid": RUN_ID_2, "vid": VERSION_ID},
            )
        return real_evaluate(pure_input, evaluation_as_of=evaluation_as_of)

    monkeypatch.setattr(filter_service, "evaluate_pure", mutate_run_between_read_and_persist)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_run (id, run_kind, status, config_version, started_at) "
                    "VALUES (:id, 'daily', 'running', 'task008-test', :started_at)"
                ),
                {"id": RUN_ID_2, "started_at": BASE},
            )
        with pytest.raises(StaleFilterInputError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.INPUT_STALE
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_missing_observed_run_is_persisted_input_invalid(postgres_url: str, monkeypatch) -> None:
    engine = create_engine(postgres_url)
    real_load = filter_service._load_snapshot

    def dangling_then_load(engine, posting_id):
        _dangling_run_reference(engine)
        return real_load(engine, posting_id)

    monkeypatch.setattr(filter_service, "_load_snapshot", dangling_then_load)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        with pytest.raises(FilterPersistedInputError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.PERSISTED_INPUT_INVALID
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _restore_run_reference(engine)
        _cleanup(engine)
        engine.dispose()


def test_lock_sequence_is_table_then_linked_score_then_posting_version_company(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if (
            normalized.startswith("lock table digest_item")
            or " for share" in normalized
            or " for update" in normalized
        ):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        _insert_digest_action(engine, state="skipped")
        result = _evaluate(engine)
        assert HardFilterReason.ALREADY_ACTIONED in result.rejection_reasons
        assert statements[0].startswith("lock table digest_item in share mode")
        assert "select score.id" in statements[1]
        assert " for share" in statements[1]
        assert "select posting.id" in statements[2]
        assert " for update" in statements[2]
        assert "select posting_version.id" in statements[3]
        assert " for share" in statements[3]
        assert "select company.id" in statements[4]
        assert " for share" in statements[4]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        _cleanup(engine)
        engine.dispose()


def test_unlinked_filter_score_is_not_an_action_but_digest_linked_skip_is(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        first = _evaluate(engine)
        assert HardFilterReason.ALREADY_ACTIONED not in first.rejection_reasons
        _insert_digest_action(engine, state="skipped")
        second = _evaluate(engine)
        assert HardFilterReason.ALREADY_ACTIONED in second.rejection_reasons
    finally:
        _cleanup(engine)
        engine.dispose()


def test_unrelated_unique_violation_is_not_classified_as_input_hash_race(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        first = _evaluate(engine)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM hard_filter_evaluation WHERE id = :id"),
                {"id": first.evaluation_id},
            )
        # The filter-stage score for the same key already exists. Re-evaluating
        # re-inserts H but the S insert collides on the score unique key with
        # SQLSTATE 23505 — an unrelated unique violation that must NOT be
        # recovered as a hard-filter input-hash race.
        with pytest.raises(FilterDatabaseError) as caught:
            _evaluate(engine)
        assert caught.value.code is FilterErrorCode.DATABASE_ERROR
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_concurrent_identical_eligible_calls_converge_on_one_evaluation(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url, pool_size=4, max_overflow=0)
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(_evaluate(engine))
        except Exception as exc:  # noqa: BLE001 - surfaced in assertions
            errors.append(exc)

    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert errors == []
        assert sorted(result.replayed for result in results) == [False, True]
        assert len({result.evaluation_id for result in results}) == 1
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM hard_filter_evaluation WHERE posting_id = :id"),
                    {"id": POSTING_ID},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_engine_reusable_after_success_replay_failure_and_concurrency(postgres_url: str) -> None:
    engine = create_engine(postgres_url, pool_size=4, max_overflow=0)
    original_dispose = engine.dispose
    dispose_calls = 0

    def dispose_spy(*args, **kwargs):
        nonlocal dispose_calls
        dispose_calls += 1
        return original_dispose(*args, **kwargs)

    engine.dispose = dispose_spy  # type: ignore[method-assign]
    try:
        _seed(engine, title="Marketing Director", description="Remote worldwide")
        first = _evaluate(engine)
        assert first.replayed is False
        assert engine.pool.checkedout() == 0
        replay = _evaluate(engine)
        assert replay.replayed is True
        assert replay.evaluation_id == first.evaluation_id
        assert engine.pool.checkedout() == 0
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM hard_filter_evaluation WHERE posting_id = :id"),
                {"id": POSTING_ID},
            )
            conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        with pytest.raises(PostingNotFoundError):
            _evaluate(engine)
        assert engine.pool.checkedout() == 0
        _cleanup(engine)
        _seed(engine, title="Product Manager", description="Remote worldwide")
        results = []
        errors = []

        def run() -> None:
            try:
                results.append(_evaluate(engine))
            except Exception as exc:  # noqa: BLE001 - surfaced in assertions
                errors.append(exc)

        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert errors == []
        assert sorted(result.replayed for result in results) == [False, True]
        assert engine.pool.checkedout() == 0
        assert dispose_calls == 0
    finally:
        _cleanup(engine)
        engine.dispose = original_dispose  # type: ignore[method-assign]
        original_dispose()


def test_linked_score_lock_probe_proves_public_path_locks_the_score_row(postgres_url: str) -> None:
    """Regression: the public path must lock the linked ``score`` row through
    ``score.id`` — never through ``digest_item`` ids. Here ``digest_item.id``
    (880008) differs from the linked ``score.id`` (880006); while the
    evaluation holds its locks, a second connection proves the linked score
    row is genuinely locked (FOR UPDATE NOWAIT fails) while an unlinked score
    row is not."""
    engine = create_engine(postgres_url, pool_size=3, max_overflow=0)
    probe_engine = create_engine(postgres_url, pool_size=1, max_overflow=0)
    lock_acquired = threading.Event()
    release = threading.Event()
    results = []
    errors = []
    assert ACTION_SCORE_ID != DIGEST_ITEM_ID

    def run() -> None:
        try:
            results.append(_evaluate(engine))
        except Exception as exc:  # noqa: BLE001 - surfaced in assertions
            errors.append(exc)

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select score.id") and " for share" in normalized:
            lock_acquired.set()
            release.wait(timeout=20)

    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        _seed(engine, title="Product Manager", description="Remote worldwide")
        _insert_digest_action(engine, state="skipped")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO score (id, run_id, posting_version_id, stage, input_hash) "
                    "VALUES (:id, :run_id, :version_id, 'triage', :input_hash)"
                ),
                {
                    "id": SCORE_ID_2,
                    "run_id": RUN_ID,
                    "version_id": VERSION_ID,
                    "input_hash": "d" * 64,
                },
            )
        thread = threading.Thread(target=run)
        thread.start()
        assert lock_acquired.wait(timeout=20), "evaluation never locked the linked score row"
        with probe_engine.connect() as probe:
            blocked = False
            try:
                probe.execute(
                    text("SELECT id FROM score WHERE id = :sid FOR UPDATE NOWAIT"),
                    {"sid": ACTION_SCORE_ID},
                ).all()
            except Exception as exc:  # noqa: BLE001 - psycopg lock_not_available
                assert getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03"
                blocked = True
            assert blocked, "the linked score row was not locked by the evaluation"
        with probe_engine.connect() as probe:
            assert (
                probe.execute(
                    text("SELECT id FROM score WHERE id = :sid FOR UPDATE NOWAIT"),
                    {"sid": SCORE_ID_2},
                ).scalar_one()
                == SCORE_ID_2
            )
        release.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert errors == []
        assert results and HardFilterReason.ALREADY_ACTIONED in results[0].rejection_reasons
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", after_execute)
        _cleanup(engine)
        engine.dispose()
        probe_engine.dispose()
