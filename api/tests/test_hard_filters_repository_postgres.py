"""Repository-focused PostgreSQL tests for Task 008 immutable persistence.

These tests exercise only ``app.filters.repository`` — the complete validated
persistence record, the decision-11 lock order, the immutable insert, the
atomic filter-stage score projection, full replay validation, and the
authorized unique-insert race recovery. Fixtures build the canonical payloads
with the service's private payload helpers, but no service orchestration runs
under test.

Coverage maps to the checkpoint requirements:

1. valid evaluation persists and reloads exactly, with one score projection;
2. no existing row is updated during replay;
3. repeated identical persistence returns the same immutable row;
4. concurrent identical inserts produce one row and equivalent results;
5. unique-conflict recovery handles only the named ``input_hash`` conflict;
6. unrelated integrity errors are not misclassified as idempotency races;
7. tampering with every replay-protected field is detected;
8. malformed and legacy enum values are detected;
9. rule-order, projection, eligibility, and hash disagreement are detected;
10. ownership mismatch is detected;
11. rollback leaves no partial row or score;
12. error messages carry only approved closed codes;
13. repository execution makes no model, network, filesystem, or clock call;
14. ``score`` is read for authoritative action state and written exactly once
    for a completed rejected evaluation, never updated or deleted;
15. no evaluation row is updated or deleted.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, event, text

import app.filters.repository as repository
import app.filters.service as filter_service  # fixture construction only
from app.filters.evaluator import PureFilterInput, StalenessSource, evaluate_pure
from app.filters.policy import POLICY_MANIFEST_HASH, POLICY_VERSION, manifest_copy
from app.filters.repository import EvaluationRecord, persist_or_replay
from app.filters.results import (
    CurrentVersionError,
    FilterDatabaseError,
    FilterError,
    FilterErrorCode,
    FilterEvaluationConflictError,
    FilterPersistedInputError,
    PostingNotFoundError,
    StaleFilterInputError,
)

pytestmark = pytest.mark.postgres

AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)
COMPANY_ID = 880001
ENDPOINT_ID = 880002
RUN_ID = 880003
RUN_ID_2 = 880013
POSTING_ID = 880004
VERSION_ID = 880005
COMPANY_ID_2 = 880101
ENDPOINT_ID_2 = 880102
POSTING_ID_2 = 880104
VERSION_ID_2 = 880105
VERSION_ID_ALT = 880205
SCORE_ID = 880006
SCORE_ID_2 = 880014
DIGEST_ID = 880007
DIGEST_ITEM_ID = 880008

_TITLE = "Marketing Director"
_DESCRIPTION = "Remote worldwide"

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
        conn.execute(text("DELETE FROM hard_filter_evaluation"))
        conn.execute(text("DELETE FROM digest_item WHERE id = :id"), {"id": DIGEST_ITEM_ID})
        conn.execute(text("DELETE FROM daily_digest WHERE id = :id"), {"id": DIGEST_ID})
        conn.execute(text("DELETE FROM score WHERE id = :id"), {"id": SCORE_ID})
        conn.execute(text("DELETE FROM score WHERE id = :id"), {"id": SCORE_ID_2})
        conn.execute(
            text("DELETE FROM score WHERE posting_version_id IN (:v1, :v2, :v3)"),
            {"v1": VERSION_ID, "v2": VERSION_ID_2, "v3": VERSION_ID_ALT},
        )
        conn.execute(text("DELETE FROM posting_version WHERE id = :id"), {"id": VERSION_ID})
        conn.execute(text("DELETE FROM posting_version WHERE id = :id"), {"id": VERSION_ID_2})
        conn.execute(text("DELETE FROM posting_version WHERE id = :id"), {"id": VERSION_ID_ALT})
        conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID_2})
        conn.execute(text("DELETE FROM source_endpoint WHERE id = :id"), {"id": ENDPOINT_ID})
        conn.execute(text("DELETE FROM source_endpoint WHERE id = :id"), {"id": ENDPOINT_ID_2})
        conn.execute(text("DELETE FROM pipeline_run WHERE id = :id"), {"id": RUN_ID})
        conn.execute(text("DELETE FROM pipeline_run WHERE id = :id"), {"id": RUN_ID_2})
        conn.execute(text("DELETE FROM company WHERE id = :id"), {"id": COMPANY_ID})
        conn.execute(text("DELETE FROM company WHERE id = :id"), {"id": COMPANY_ID_2})


def _seed(engine, *, title: str = _TITLE, description: str = _DESCRIPTION) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO company (id, name, blocked) "
                "VALUES (:id, 'Invented Company One', false)"
            ),
            {"id": COMPANY_ID},
        )
        conn.execute(
            text(
                "INSERT INTO company (id, name, blocked) "
                "VALUES (:id, 'Invented Company Two', false)"
            ),
            {"id": COMPANY_ID_2},
        )
        conn.execute(
            text(
                "INSERT INTO source_endpoint (id, company_id, kind, token) "
                "VALUES (:id, :cid, 'custom', 'tok-one')"
            ),
            {"id": ENDPOINT_ID, "cid": COMPANY_ID},
        )
        conn.execute(
            text(
                "INSERT INTO source_endpoint (id, company_id, kind, token) "
                "VALUES (:id, :cid, 'custom', 'tok-two')"
            ),
            {"id": ENDPOINT_ID_2, "cid": COMPANY_ID_2},
        )
        conn.execute(
            text(
                "INSERT INTO pipeline_run (id, run_kind, status, config_version, started_at) "
                "VALUES (:id, 'manual', 'succeeded', 'repo-test', now())"
            ),
            {"id": RUN_ID},
        )
        conn.execute(
            text(
                "INSERT INTO posting (id, company_id, source_endpoint_id, external_id, title, "
                "title_norm, posting_url, apply_url, first_seen_at) VALUES "
                "(:id, :cid, :eid, 'ext-one', 'Invented Title One', 'invented title one', "
                "'https://example.invalid/one', 'https://example.invalid/apply-one', :fs)"
            ),
            {"id": POSTING_ID, "cid": COMPANY_ID, "eid": ENDPOINT_ID, "fs": AS_OF},
        )
        conn.execute(
            text(
                "INSERT INTO posting (id, company_id, source_endpoint_id, external_id, title, "
                "title_norm, posting_url, apply_url, first_seen_at) VALUES "
                "(:id, :cid, :eid, 'ext-two', 'Invented Title Two', 'invented title two', "
                "'https://example.invalid/two', 'https://example.invalid/apply-two', :fs)"
            ),
            {"id": POSTING_ID_2, "cid": COMPANY_ID_2, "eid": ENDPOINT_ID_2, "fs": AS_OF},
        )
        conn.execute(
            text(
                "INSERT INTO posting_version (id, posting_id, observed_in_run_id, content_hash, "
                "title, description_md, raw_payload) VALUES "
                "(:id, :pid, :rid, :ch, :title, :description, '{}'::jsonb)"
            ),
            {
                "id": VERSION_ID,
                "pid": POSTING_ID,
                "rid": RUN_ID,
                "ch": "b" * 64,
                "title": title,
                "description": description,
            },
        )
        conn.execute(
            text(
                "INSERT INTO posting_version (id, posting_id, observed_in_run_id, content_hash, "
                "title, description_md, raw_payload) VALUES "
                "(:id, :pid, :rid, :ch, 'Invented Title Two', 'Remote worldwide', '{}'::jsonb)"
            ),
            {"id": VERSION_ID_2, "pid": POSTING_ID_2, "rid": RUN_ID, "ch": "c" * 64},
        )
        conn.execute(
            text("UPDATE posting SET current_version_id = :vid WHERE id = :pid"),
            {"vid": VERSION_ID, "pid": POSTING_ID},
        )


def _snapshot(title: str = _TITLE, description: str = _DESCRIPTION) -> dict[str, Any]:
    return {
        "posting": {
            "id": POSTING_ID,
            "company_id": COMPANY_ID,
            "source_published_at": None,
            "first_seen_at": AS_OF,
            "closed_at": None,
            "current_version_id": VERSION_ID,
        },
        "version": {
            "id": VERSION_ID,
            "posting_id": POSTING_ID,
            "observed_in_run_id": RUN_ID,
            "content_hash": "b" * 64,
            "title": title,
            "locations": [],
            "description_md": description,
        },
        "company": {"blocked": False},
    }


def _pure(snapshot: dict[str, Any]):
    posting = snapshot["posting"]
    version = snapshot["version"]
    company = snapshot["company"]
    return evaluate_pure(
        PureFilterInput(
            posting_id=posting["id"],
            posting_version_id=version["id"],
            content_hash=version["content_hash"],
            title=version["title"],
            description_md=version["description_md"],
            locations=tuple(version["locations"]),
            source_published_at=posting["source_published_at"],
            first_seen_at=posting["first_seen_at"],
            current_version_id=posting["current_version_id"],
            closed_at=posting["closed_at"],
            company_blocked=company["blocked"],
            action_state=(),
        ),
        evaluation_as_of=AS_OF,
    )


def _record(title: str = _TITLE, description: str = _DESCRIPTION) -> EvaluationRecord:
    snapshot = _snapshot(title, description)
    pure = _pure(snapshot)
    input_payload, input_hash, mutable_hash = filter_service._input_payload(
        snapshot, (), policy_version=POLICY_VERSION, as_of=AS_OF
    )
    output_payload, result_hash = filter_service._output_payload(pure)
    return EvaluationRecord(
        posting_id=snapshot["posting"]["id"],
        posting_version_id=snapshot["version"]["id"],
        policy_version=POLICY_VERSION,
        policy_manifest_hash=POLICY_MANIFEST_HASH,
        mutable_state_hash=mutable_hash,
        result_hash=result_hash,
        input_hash=input_hash,
        policy_manifest=manifest_copy(),
        input_payload=input_payload,
        output_payload=output_payload,
        evaluation_as_of=AS_OF,
        eligible=pure.eligible,
        rejection_reasons=pure.rejection_reasons,
        unknowns=pure.unknowns,
        staleness_source=pure.staleness_source,
        rule_outcomes=pure.rule_outcomes,
        observed_run_id=snapshot["version"]["observed_in_run_id"],
    )


def _assert_closed(error: Exception, code: FilterErrorCode) -> None:
    assert isinstance(error, FilterError)
    assert error.code is code
    assert str(error) == code.value
    for marker in (_TITLE, _DESCRIPTION, "example.invalid", "b" * 64, "phase1-hard-filters-v1"):
        assert marker not in str(error)


def _evaluation_row(engine, input_hash: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM hard_filter_evaluation WHERE input_hash = :h"),
                {"h": input_hash},
            )
            .mappings()
            .one()
        )
        return dict(row)


def _filter_score_row(engine, posting_version_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM score WHERE posting_version_id = :id AND stage = 'filter'"),
                {"id": posting_version_id},
            )
            .mappings()
            .one()
        )
        return dict(row)


def _assert_projection(row: dict[str, Any], record: EvaluationRecord) -> None:
    assert row["run_id"] == record.observed_run_id
    assert row["posting_version_id"] == record.posting_version_id
    assert row["stage"] == "filter"
    assert row["ruleset_version"] == record.policy_version
    assert row["input_hash"] == record.input_hash
    assert row["reject_reasons"] == [reason.value for reason in record.rejection_reasons]
    assert row["scored_at"] == record.evaluation_as_of
    assert all(row[column] is None for column in _UNRELATED_SCORE_COLUMNS)


def _dangling_run_reference(engine, *, version_id: int, run_id: int) -> None:
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
            {"rid": run_id, "id": version_id},
        )


def _restore_run_reference(engine, *, version_id: int, run_id: int) -> None:
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
            {"rid": run_id, "id": version_id},
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


# --------------------------------------------------------------------------
# 1-3. Persistence, exact reload, immutability.
# --------------------------------------------------------------------------


def test_valid_evaluation_persists_and_reloads_exactly(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        assert first.evaluation_id > 0
        row = _evaluation_row(engine, record.input_hash)
        assert row["posting_id"] == record.posting_id
        assert row["posting_version_id"] == record.posting_version_id
        assert row["policy_version"] == record.policy_version
        assert row["policy_manifest_hash"] == record.policy_manifest_hash
        assert row["mutable_state_hash"] == record.mutable_state_hash
        assert row["result_hash"] == record.result_hash
        assert row["input_hash"] == record.input_hash
        assert row["policy_manifest"] == record.policy_manifest
        assert row["input_payload"] == record.input_payload
        assert row["output_payload"] == record.output_payload
        assert row["evaluation_as_of"] == record.evaluation_as_of
        assert row["eligible"] == record.eligible
        assert row["rejection_reasons"] == [r.value for r in record.rejection_reasons]
        assert row["unknowns"] == [u.value for u in record.unknowns]
        assert row["staleness_source"] == record.staleness_source.value
        assert row["rule_outcomes"] == [
            {"evidence": o.evidence.value, "rule": o.rule.value, "status": o.status.value}
            for o in record.rule_outcomes
        ]
        replay = persist_or_replay(engine=engine, record=record)
        assert replay.replayed is True
        assert replay.evaluation_id == first.evaluation_id
        assert replay.posting_id == first.posting_id
        assert replay.posting_version_id == first.posting_version_id
        assert replay.policy_manifest_hash == first.policy_manifest_hash
        assert replay.mutable_state_hash == first.mutable_state_hash
        assert replay.input_hash == first.input_hash
        assert replay.result_hash == first.result_hash
        assert replay.evaluation_as_of == first.evaluation_as_of
        assert replay.eligible == first.eligible
        assert replay.rejection_reasons == first.rejection_reasons
        assert replay.unknowns == first.unknowns
        assert replay.staleness_source == first.staleness_source
        assert replay.rule_outcomes == first.rule_outcomes
        score_row = _filter_score_row(engine, VERSION_ID)
        _assert_projection(score_row, record)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_replay_never_updates_the_existing_row(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        before = _evaluation_row(engine, record.input_hash)
        score_before = _filter_score_row(engine, VERSION_ID)
        replay = persist_or_replay(engine=engine, record=record)
        assert replay.replayed is True
        after = _evaluation_row(engine, record.input_hash)
        score_after = _filter_score_row(engine, VERSION_ID)
        assert before == after
        assert score_before == score_after
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_repeated_identical_persistence_returns_same_immutable_row(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        results = [persist_or_replay(engine=engine, record=record) for _ in range(3)]
        assert [result.replayed for result in results] == [False, True, True]
        assert len({result.evaluation_id for result in results}) == 1
        rows = [_evaluation_row(engine, record.input_hash) for _ in range(2)]
        assert rows[0] == rows[1]
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# 4-6. Concurrency: convergence and the named unique-conflict recovery.
# --------------------------------------------------------------------------


def test_concurrent_identical_inserts_converge_on_one_row(postgres_url: str) -> None:
    engine = create_engine(postgres_url, pool_size=2, max_overflow=0)
    results = []
    errors = []
    barrier = threading.Barrier(2)
    record = _record()

    def run() -> None:
        try:
            barrier.wait()
            results.append(persist_or_replay(engine=engine, record=record))
        except Exception as exc:  # noqa: BLE001 - surfaced in assertions
            errors.append(exc)

    try:
        _cleanup(engine)
        _seed(engine)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        assert errors == []
        assert sorted(result.replayed for result in results) == [False, True]
        assert results[0].evaluation_id == results[1].evaluation_id
        assert results[0].posting_version_id == results[1].posting_version_id
        assert results[0].input_hash == results[1].input_hash
        assert results[0].result_hash == results[1].result_hash
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
        _assert_projection(_filter_score_row(engine, VERSION_ID), record)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_unique_race_recovery_validates_the_winner(postgres_url: str, monkeypatch) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        real_load = repository._load_by_input_hash
        real_insert = repository._insert_evaluation
        blind = {"active": True}

        def blinded_load(conn, input_hash):
            if blind["active"]:
                return None
            return real_load(conn, input_hash)

        def insert_then_unblind(conn, record):
            blind["active"] = False
            return real_insert(conn, record)

        monkeypatch.setattr(repository, "_load_by_input_hash", blinded_load)
        monkeypatch.setattr(repository, "_insert_evaluation", insert_then_unblind)
        recovered = persist_or_replay(engine=engine, record=record)
        assert recovered.replayed is True
        assert recovered.evaluation_id == first.evaluation_id
        assert recovered.input_hash == first.input_hash
        assert recovered.result_hash == first.result_hash
        assert recovered.rejection_reasons == first.rejection_reasons
        assert recovered.unknowns == first.unknowns
        assert recovered.rule_outcomes == first.rule_outcomes
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
        _assert_projection(_filter_score_row(engine, VERSION_ID), record)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_unrelated_integrity_errors_are_not_misclassified(postgres_url: str, monkeypatch) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        monkeypatch.setattr(
            repository,
            "_lock_and_recheck",
            lambda conn, record: (None, None, None, []),
        )
        with pytest.raises(FilterDatabaseError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.DATABASE_ERROR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# 7-10. Replay validation: tampering, malformed/legacy values, order,
#       projections, eligibility, and ownership.
# --------------------------------------------------------------------------


_TAMPER_SQL: dict[str, Callable[[EvaluationRecord], tuple[str, dict[str, Any]]]] = {
    "policy_version": lambda r: (
        "UPDATE hard_filter_evaluation SET policy_version = :v WHERE input_hash = :h",
        {"v": "phase1-hard-filters-v2"},
    ),
    "policy_manifest_hash": lambda r: (
        "UPDATE hard_filter_evaluation SET policy_manifest_hash = :v WHERE input_hash = :h",
        {"v": "f" * 64},
    ),
    "mutable_state_hash": lambda r: (
        "UPDATE hard_filter_evaluation SET mutable_state_hash = :v WHERE input_hash = :h",
        {"v": "f" * 64},
    ),
    "result_hash": lambda r: (
        "UPDATE hard_filter_evaluation SET result_hash = :v WHERE input_hash = :h",
        {"v": "f" * 64},
    ),
    "policy_manifest": lambda r: (
        'UPDATE hard_filter_evaluation SET policy_manifest = \'{"version":"tampered"}\'::jsonb '
        "WHERE input_hash = :h",
        {},
    ),
    "input_payload": lambda r: (
        "UPDATE hard_filter_evaluation SET input_payload = "
        "input_payload || jsonb_build_object('extra', 1) WHERE input_hash = :h",
        {},
    ),
    "output_payload_eligible": lambda r: (
        "UPDATE hard_filter_evaluation SET output_payload = "
        "jsonb_set(output_payload, '{eligible}', 'true') WHERE input_hash = :h",
        {},
    ),
    "evaluation_as_of": lambda r: (
        "UPDATE hard_filter_evaluation SET evaluation_as_of = '2026-08-01T13:00:00+00:00' "
        "WHERE input_hash = :h",
        {},
    ),
    "rejection_reasons": lambda r: (
        "UPDATE hard_filter_evaluation SET rejection_reasons = "
        '\'["title_no_match","stale"]\'::jsonb WHERE input_hash = :h',
        {},
    ),
    "unknowns": lambda r: (
        "UPDATE hard_filter_evaluation SET unknowns = "
        "'[\"remote_arrangement_unresolved\"]'::jsonb WHERE input_hash = :h",
        {},
    ),
    "staleness_source": lambda r: (
        "UPDATE hard_filter_evaluation SET staleness_source = :v WHERE input_hash = :h",
        {
            "v": "source_published_at"
            if r.staleness_source is StalenessSource.FIRST_SEEN_AT
            else "first_seen_at"
        },
    ),
}


@pytest.mark.parametrize("label", sorted(_TAMPER_SQL))
def test_replay_detects_tampered_field(postgres_url: str, label: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        sql, params = _TAMPER_SQL[label](record)
        with engine.begin() as conn:
            conn.execute(text(sql), {"h": record.input_hash, **params})
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_replay_detects_input_hash_column_tamper(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE hard_filter_evaluation SET input_hash = :v WHERE id = :id"),
                {"v": "f" * 64, "id": first.evaluation_id},
            )
        tampered = replace(record, input_hash="f" * 64)
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=tampered)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def _tamper_output(
    engine, evaluation_id: int, mutation: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """Apply a mutation to the persisted output payload and recompute the
    duplicated columns and ``result_hash`` consistently, so only the intended
    corruption remains and replay reaches the deep output validator."""
    with engine.connect() as conn:
        output = conn.execute(
            text("SELECT output_payload FROM hard_filter_evaluation WHERE id = :id"),
            {"id": evaluation_id},
        ).scalar_one()
    tampered = mutation(output)
    serialized = json.dumps(
        tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    result_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hard_filter_evaluation SET output_payload = CAST(:op AS jsonb), "
                "result_hash = :rh, rejection_reasons = CAST(:rr AS jsonb), "
                "unknowns = CAST(:un AS jsonb), rule_outcomes = CAST(:ro AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "op": json.dumps(tampered),
                "rh": result_hash,
                "rr": json.dumps(tampered["rejection_reasons"]),
                "un": json.dumps(tampered["unknowns"]),
                "ro": json.dumps(tampered["rule_outcomes"]),
                "id": evaluation_id,
            },
        )
    return tampered


_MUTATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "legacy_discipline_unknown": lambda o: {**o, "unknowns": ["discipline_unknown"]},
    "arbitrary_unknown_enum": lambda o: {**o, "unknowns": ["bogus_unknown"]},
    "duplicate_unknowns": lambda o: {
        **o,
        "unknowns": ["clearance_not_stated", "clearance_not_stated"],
    },
    "wrong_unknown_ordering": lambda o: {
        **o,
        "unknowns": ["clearance_ambiguous", "clearance_not_stated"],
    },
    "duplicate_reasons": lambda o: {
        **o,
        "rejection_reasons": ["title_no_match", "title_no_match"],
    },
    "wrong_reason_ordering": lambda o: {
        **o,
        "rejection_reasons": ["stale", "title_no_match"],
    },
    "arbitrary_rule_enum": lambda o: {
        **o,
        "rule_outcomes": [
            {**o["rule_outcomes"][0], "rule": "bogus_rule"},
            *o["rule_outcomes"][1:],
        ],
    },
    "arbitrary_status_enum": lambda o: {
        **o,
        "rule_outcomes": [
            {**o["rule_outcomes"][0], "status": "bogus_status"},
            *o["rule_outcomes"][1:],
        ],
    },
    "arbitrary_evidence_enum": lambda o: {
        **o,
        "rule_outcomes": [
            {**o["rule_outcomes"][0], "evidence": "bogus_evidence"},
            *o["rule_outcomes"][1:],
        ],
    },
    "missing_status_key": lambda o: {
        **o,
        "rule_outcomes": [
            {
                "rule": o["rule_outcomes"][0]["rule"],
                "evidence": o["rule_outcomes"][0]["evidence"],
            },
            *o["rule_outcomes"][1:],
        ],
    },
    "wrong_rule_ordering": lambda o: {
        **o,
        "rule_outcomes": [*o["rule_outcomes"][1:], o["rule_outcomes"][0]],
    },
    "illegal_triple": lambda o: {
        **o,
        "rule_outcomes": [
            {**o["rule_outcomes"][0], "evidence": "attendance_required"},
            *o["rule_outcomes"][1:],
        ],
    },
}


@pytest.mark.parametrize("label", sorted(_MUTATIONS))
def test_replay_reaches_deep_output_validators(postgres_url: str, label: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        tampered = _tamper_output(engine, first.evaluation_id, _MUTATIONS[label])
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=replace(record, output_payload=tampered))
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_replay_detects_malformed_input_payload(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE hard_filter_evaluation SET input_payload = "
                    "jsonb_set(input_payload, '{mutable_state}', '[]') WHERE input_hash = :h"
                ),
                {"h": record.input_hash},
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_replay_detects_projection_disagreement(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE hard_filter_evaluation SET output_payload = "
                    "jsonb_set(output_payload, '{rejection_reasons}', '[]') WHERE input_hash = :h"
                ),
                {"h": record.input_hash},
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_replay_detects_eligibility_disagreement(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        tampered = _tamper_output(engine, first.evaluation_id, lambda o: {**o, "eligible": True})
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=replace(record, output_payload=tampered))
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_ownership_mismatch_is_detected(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE hard_filter_evaluation SET posting_id = :pid, "
                    "posting_version_id = :vid WHERE id = :id"
                ),
                {"pid": POSTING_ID_2, "vid": VERSION_ID_2, "id": first.evaluation_id},
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
    finally:
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# 11-12. Rollback atomicity and the closed error surface.
# --------------------------------------------------------------------------


def test_failed_persistence_rolls_back_without_partial_row(postgres_url: str, monkeypatch) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        real_insert = repository._insert_evaluation
        calls = {"n": 0}

        def insert_then_fail_once(conn, record):
            calls["n"] += 1
            if calls["n"] == 1:
                real_insert(conn, record)
                raise RuntimeError("injected post-insert failure")
            return real_insert(conn, record)

        monkeypatch.setattr(repository, "_insert_evaluation", insert_then_fail_once)
        with pytest.raises(RuntimeError):
            persist_or_replay(engine=engine, record=record)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
        result = persist_or_replay(engine=engine, record=record)
        assert result.replayed is False
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_error_messages_are_closed_codes_only(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE hard_filter_evaluation SET result_hash = :v WHERE input_hash = :h"),
                {"v": "f" * 64, "h": record.input_hash},
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE posting SET first_seen_at = :ts WHERE id = :pid"),
                {"ts": AS_OF.replace(minute=13), "pid": POSTING_ID},
            )
        with pytest.raises(StaleFilterInputError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.INPUT_STALE)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM hard_filter_evaluation"))
            conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        with pytest.raises(PostingNotFoundError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.POSTING_NOT_FOUND)
    finally:
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# 13-15. Behavioral guarantees: no external calls, score read-only, no
#        evaluation-row mutation.
# --------------------------------------------------------------------------


def test_repository_execution_makes_no_network_filesystem_or_clock_call(
    postgres_url: str, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("blocked external call")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(httpx.Client, "request", fail)
    monkeypatch.setattr("builtins.open", fail)
    source = inspect.getsource(repository)
    assert "datetime.now" not in source
    assert "utcnow" not in source
    assert "time.time" not in source
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        result = persist_or_replay(engine=engine, record=_record())
        assert result.replayed is False
    finally:
        _cleanup(engine)
        engine.dispose()


def test_score_written_once_for_rejected_and_never_updated_or_deleted(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    statements: list[tuple[str, dict[str, Any]]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if isinstance(parameters, dict):
            params = dict(parameters)
        else:
            params = dict(parameters[0]) if parameters else {}
        statements.append((" ".join(statement.lower().split()), params))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        _cleanup(engine)
        _seed(engine)
        statements.clear()
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        before = _evaluation_row(engine, record.input_hash)
        score_before = _filter_score_row(engine, VERSION_ID)
        replay = persist_or_replay(engine=engine, record=record)
        assert replay.replayed is True
        assert replay.evaluation_id == first.evaluation_id
        after = _evaluation_row(engine, record.input_hash)
        score_after = _filter_score_row(engine, VERSION_ID)
        assert before == after
        assert score_before == score_after
        score_inserts = [
            statement for statement, _ in statements if statement.startswith("insert into score")
        ]
        assert len(score_inserts) == 1, "a rejected evaluation inserts exactly one score"
        insert_statement, insert_params = next(
            (statement, params)
            for statement, params in statements
            if statement.startswith("insert into score")
        )
        assert "stage" in insert_statement
        assert insert_params.get("stage") == "filter"
        assert insert_params.get("ruleset_version") == record.policy_version
        assert insert_params.get("input_hash") == record.input_hash
        assert insert_params.get("run_id") == record.observed_run_id
        assert insert_params.get("posting_version_id") == record.posting_version_id
        forbidden = [
            statement
            for statement, _ in statements
            if statement.startswith(("update score", "delete from score"))
            or statement.startswith(
                ("update hard_filter_evaluation", "delete from hard_filter_evaluation")
            )
        ]
        assert forbidden == [], forbidden
        score_reads = [statement for statement, _ in statements if "score" in statement]
        assert score_reads, "the action-facts join must read score"
        assert all(statement.startswith(("select", "insert")) for statement in score_reads)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 1
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# Loading semantics: stale pointers, missing state, corrupt relationships.
# --------------------------------------------------------------------------


def test_stale_current_version_pointer_is_detected(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO posting_version (id, posting_id, observed_in_run_id, "
                    "content_hash, title, raw_payload) VALUES "
                    "(:id, :pid, :rid, :ch, 'Changed Title', '{}'::jsonb)"
                ),
                {"id": VERSION_ID_ALT, "pid": POSTING_ID, "rid": RUN_ID, "ch": "a" * 64},
            )
            conn.execute(
                text("UPDATE posting SET current_version_id = :vid WHERE id = :pid"),
                {"vid": VERSION_ID_ALT, "pid": POSTING_ID},
            )
        with pytest.raises(StaleFilterInputError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.INPUT_STALE)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_missing_posting_is_posting_not_found(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM posting WHERE id = :id"), {"id": POSTING_ID})
        with pytest.raises(PostingNotFoundError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.POSTING_NOT_FOUND)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_missing_current_version_pointer_is_current_version_invalid(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE posting SET current_version_id = NULL WHERE id = :pid"),
                {"pid": POSTING_ID},
            )
        with pytest.raises(CurrentVersionError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.CURRENT_VERSION_INVALID)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_corrupt_action_relationship_is_persisted_input_invalid(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO score (id, run_id, posting_version_id, stage, input_hash) "
                    "VALUES (:id, :rid, :vid, 'triage', :h)"
                ),
                {"id": SCORE_ID, "rid": RUN_ID, "vid": VERSION_ID_2, "h": "c" * 64},
            )
            conn.execute(
                text("INSERT INTO daily_digest (id, digest_on, run_id) " "VALUES (:id, :d, :rid)"),
                {"id": DIGEST_ID, "d": AS_OF.date(), "rid": RUN_ID},
            )
            conn.execute(
                text(
                    "INSERT INTO digest_item (id, digest_id, posting_id, score_id, rank, state) "
                    "VALUES (:id, :did, :pid, :sid, 1, 'recommended')"
                ),
                {"id": DIGEST_ITEM_ID, "did": DIGEST_ID, "pid": POSTING_ID, "sid": SCORE_ID},
            )
        with pytest.raises(FilterPersistedInputError) as caught:
            persist_or_replay(engine=engine, record=_record())
        _assert_closed(caught.value, FilterErrorCode.PERSISTED_INPUT_INVALID)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
    finally:
        _cleanup(engine)
        engine.dispose()


# --------------------------------------------------------------------------
# Filter-stage score projection: persistence, replay, and tamper detection.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "run_id",
        "ruleset_version",
        "reject_reasons",
        "scored_at",
        "unrelated_non_null",
        "input_hash",
    ],
)
def test_rejected_replay_requires_exactly_one_matching_score(
    postgres_url: str, mutation: str
) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        _assert_projection(_filter_score_row(engine, VERSION_ID), record)
        with engine.begin() as conn:
            if mutation == "missing":
                conn.execute(
                    text("DELETE FROM score WHERE posting_version_id = :id AND stage = 'filter'"),
                    {"id": VERSION_ID},
                )
            elif mutation == "run_id":
                conn.execute(
                    text(
                        "INSERT INTO pipeline_run (id, run_kind, status, config_version, "
                        "started_at) VALUES (:id, 'manual', 'succeeded', 'repo-test', now())"
                    ),
                    {"id": RUN_ID_2},
                )
                conn.execute(
                    text(
                        "UPDATE score SET run_id = :rid "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"rid": RUN_ID_2, "id": VERSION_ID},
                )
            elif mutation == "ruleset_version":
                conn.execute(
                    text(
                        "UPDATE score SET ruleset_version = :v "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"v": "phase1-hard-filters-v2", "id": VERSION_ID},
                )
            elif mutation == "reject_reasons":
                conn.execute(
                    text(
                        "UPDATE score SET reject_reasons = CAST(:rr AS jsonb) "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"rr": json.dumps(["seniority_low"]), "id": VERSION_ID},
                )
            elif mutation == "scored_at":
                conn.execute(
                    text(
                        "UPDATE score SET scored_at = :ts "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"ts": AS_OF.replace(hour=13), "id": VERSION_ID},
                )
            elif mutation == "unrelated_non_null":
                conn.execute(
                    text(
                        "UPDATE score SET verdict = :v "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"v": "apply", "id": VERSION_ID},
                )
            else:
                conn.execute(
                    text(
                        "UPDATE score SET input_hash = :h "
                        "WHERE posting_version_id = :id AND stage = 'filter'"
                    ),
                    {"h": "f" * 64, "id": VERSION_ID},
                )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.connect() as conn:
            remaining = conn.execute(
                text(
                    "SELECT count(*) FROM score WHERE posting_version_id = :id AND stage = 'filter'"
                ),
                {"id": VERSION_ID},
            ).scalar_one()
            assert remaining == (
                0 if mutation == "missing" else 1
            ), "a missing or corrupted score must never be repaired"
    finally:
        _cleanup(engine)
        engine.dispose()


def test_eligible_record_persists_no_score_and_unexpected_score_conflicts(
    postgres_url: str,
) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record(title="Product Manager")
        assert record.eligible is True
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 0
            )
        replay = persist_or_replay(engine=engine, record=record)
        assert replay.replayed is True
        assert replay.evaluation_id == first.evaluation_id
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO score (id, run_id, posting_version_id, stage, ruleset_version, "
                    "input_hash, reject_reasons, scored_at) VALUES "
                    "(:id, :rid, :vid, 'filter', :rv, :ih, '[]'::jsonb, :ts)"
                ),
                {
                    "id": SCORE_ID_2,
                    "rid": RUN_ID,
                    "vid": VERSION_ID,
                    "rv": record.policy_version,
                    "ih": record.input_hash,
                    "ts": record.evaluation_as_of,
                },
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM hard_filter_evaluation WHERE id = :id"),
                {"id": first.evaluation_id},
            )
        with pytest.raises(FilterEvaluationConflictError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.EVALUATION_CONFLICT)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_score_unique_violation_is_not_input_hash_race(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM hard_filter_evaluation WHERE id = :id"),
                {"id": first.evaluation_id},
            )
        # Re-persisting the same rejected identity inserts H but collides on
        # the score unique key with SQLSTATE 23505. The authorized race
        # recovery must NOT treat this score-key violation as an input-hash
        # race; it stays a closed database error and persists nothing.
        with pytest.raises(FilterDatabaseError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.DATABASE_ERROR)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM score WHERE posting_version_id = :id "
                        "AND stage = 'filter'"
                    ),
                    {"id": VERSION_ID},
                ).scalar_one()
                == 1
            )
    finally:
        _cleanup(engine)
        engine.dispose()


def test_stale_observed_run_id_is_input_stale(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = _record()
        first = persist_or_replay(engine=engine, record=record)
        assert first.replayed is False
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_run (id, run_kind, status, config_version, started_at) "
                    "VALUES (:id, 'manual', 'succeeded', 'repo-test', now())"
                ),
                {"id": RUN_ID_2},
            )
            conn.execute(
                text("UPDATE posting_version SET observed_in_run_id = :rid WHERE id = :id"),
                {"rid": RUN_ID_2, "id": VERSION_ID},
            )
        with pytest.raises(StaleFilterInputError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.INPUT_STALE)
    finally:
        _cleanup(engine)
        engine.dispose()


def test_missing_observed_run_is_persisted_input_invalid(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        _cleanup(engine)
        _seed(engine)
        record = replace(_record(), observed_run_id=RUN_ID + 999)
        _dangling_run_reference(engine, version_id=VERSION_ID, run_id=RUN_ID + 999)
        with pytest.raises(FilterPersistedInputError) as caught:
            persist_or_replay(engine=engine, record=record)
        _assert_closed(caught.value, FilterErrorCode.PERSISTED_INPUT_INVALID)
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM hard_filter_evaluation")).scalar_one() == 0
            )
    finally:
        _restore_run_reference(engine, version_id=VERSION_ID, run_id=RUN_ID)
        _cleanup(engine)
        engine.dispose()
