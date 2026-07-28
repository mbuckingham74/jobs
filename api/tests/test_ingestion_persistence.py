"""PostgreSQL 16 integration coverage for idempotent ATS ingestion."""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from types import MethodType

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.ingestion import IngestionDatabaseError, IngestionService, TransportFailureCode
from app.ingestion.repository import posting, posting_version, source_endpoint, source_fetch
from app.ingestion.service import INVALID_OBSERVATION, STALE_OBSERVATION, SUSPICIOUS_ZERO
from app.sources.ats.contracts import FetchResult, RawLocation, RawPosting

pytestmark = pytest.mark.postgres

BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
MALFORMED_TIMEZONE_ERROR = "malformed timezone offset"
MALFORMED_TIMEZONE_SENTINEL = "sensitive-persistence-timezone-sentinel"


class _MalformedTimezone(tzinfo):
    def utcoffset(self, value: datetime | None):
        raise RuntimeError(f"{MALFORMED_TIMEZONE_ERROR}: {MALFORMED_TIMEZONE_SENTINEL}")


def _seed_prerequisites(
    engine,
    *,
    endpoint_ids: tuple[int, ...] = (101,),
    run_ids: tuple[int, ...] = (201,),
    endpoint_status: str = "active",
) -> None:
    with engine.begin() as conn:
        for offset, endpoint_id in enumerate(endpoint_ids):
            company_id = endpoint_id + 1000
            conn.execute(
                text("INSERT INTO company (id, name) VALUES (:id, :name)"),
                {"id": company_id, "name": f"InventedCo{offset}"},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO source_endpoint
                        (id, company_id, kind, token, region, status)
                    VALUES (:id, :company_id, 'greenhouse', :token, 'global', :status)
                    """
                ),
                {
                    "id": endpoint_id,
                    "company_id": company_id,
                    "token": f"invented-{endpoint_id}",
                    "status": endpoint_status,
                },
            )
        for run_id in run_ids:
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (id, run_kind, status, config_version, started_at)
                    VALUES (:id, 'daily', 'running', 'test', :started_at)
                    """
                ),
                {"id": run_id, "started_at": BASE},
            )


def _add_run(engine, run_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO pipeline_run
                    (id, run_kind, status, config_version, started_at)
                VALUES (:id, 'daily', 'running', 'test', :started_at)
                """
            ),
            {"id": run_id, "started_at": BASE},
        )


def _raw_posting(
    external_id: str = "job-1",
    *,
    title: str = "Product Manager",
    description: str = "Build useful things.",
    posting_url: str = "https://example.invalid/job-1",
    apply_url: str = "https://example.invalid/job-1/apply",
    department: str | None = "Product",
    raw: dict | None = None,
) -> RawPosting:
    return RawPosting(
        external_id=external_id,
        title=title,
        locations=[RawLocation(label="Remote", country_code="US", workplace_type="remote")],
        description_md=description,
        posting_url=posting_url,
        apply_url=apply_url,
        source_published_at=BASE - timedelta(days=1),
        source_updated_at=BASE,
        department=department,
        raw=raw if raw is not None else {"external": external_id, "description": description},
    )


def _fetch(
    *postings: RawPosting,
    complete: bool = True,
    status: int = 200,
    etag: str | None = '"etag"',
    last_modified: str | None = "Wed, 01 Jul 2026 12:00:00 GMT",
) -> FetchResult:
    return FetchResult(
        postings=list(postings),
        complete=complete,
        http_status=status,
        etag=etag,
        last_modified=last_modified,
    )


def _ingest(
    service: IngestionService,
    *,
    run_id: int,
    fetch: object,
    endpoint_id: int = 101,
    minute: int,
):
    started = BASE + timedelta(minutes=minute)
    return service.ingest_fetch_result(
        run_id=run_id,
        source_endpoint_id=endpoint_id,
        fetch_result=fetch,
        started_at=started,
        finished_at=started + timedelta(seconds=30),
    )


def test_new_unchanged_new_hash_and_historical_reversion(ingestion_engine) -> None:
    _seed_prerequisites(
        ingestion_engine,
        run_ids=(201, 202, 203, 204),
    )
    service = IngestionService(ingestion_engine)
    raw = {"nested": [{"sentinel": "before"}]}
    first = _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_fetch(_raw_posting(raw=raw)),
    )
    raw["nested"][0]["sentinel"] = "after"
    assert first.postings_created == 1
    assert first.versions_created == 1
    assert first.source_fetch_postings_new == 1
    assert first.source_fetch_postings_changed == 0
    first_version_id = first.created_posting_version_ids[0]

    unchanged = _ingest(
        service,
        run_id=202,
        minute=2,
        fetch=_fetch(
            _raw_posting(
                posting_url="https://example.invalid/changed-only-url",
                department="Changed-only-department",
                raw={"new": "payload"},
            )
        ),
    )
    assert unchanged.source_fetch_postings_changed == 0
    assert unchanged.versions_created == 0
    assert unchanged.postings_updated == 1  # last_seen_at only

    changed = _ingest(
        service,
        run_id=203,
        minute=3,
        fetch=_fetch(
            _raw_posting(
                description="Materially changed content.",
                posting_url="https://example.invalid/material-change",
            )
        ),
    )
    assert changed.source_fetch_postings_changed == 1
    assert changed.versions_created == 1
    assert changed.created_posting_version_ids != (first_version_id,)

    reverted = _ingest(
        service,
        run_id=204,
        minute=4,
        fetch=_fetch(
            _raw_posting(
                posting_url="https://example.invalid/reverted-projection",
                department="Reverted projection",
            )
        ),
    )
    assert reverted.source_fetch_postings_changed == 1
    assert reverted.versions_created == 0
    assert reverted.created_posting_version_ids == ()

    with ingestion_engine.connect() as conn:
        stored_posting = dict(conn.execute(select(posting)).mappings().one())
        versions = list(
            conn.execute(select(posting_version).order_by(posting_version.c.id)).mappings()
        )
    assert len(versions) == 2
    assert stored_posting["current_version_id"] == first_version_id
    assert stored_posting["posting_url"] == "https://example.invalid/reverted-projection"
    assert stored_posting["department"] == "Reverted projection"
    assert versions[0]["raw_payload"]["nested"][0]["sentinel"] == "before"

    replay = _ingest(service, run_id=204, minute=99, fetch=object())
    assert replay.replayed is True
    assert replay.source_fetch_postings_changed == 1
    assert replay.postings_created == replay.postings_updated == replay.versions_created == 0
    assert replay.created_posting_version_ids == ()


def test_incomplete_positive_304_close_and_reopen_boundaries(ingestion_engine) -> None:
    _seed_prerequisites(
        ingestion_engine,
        run_ids=(201, 202, 203, 204, 205, 206, 207),
    )
    service = IngestionService(ingestion_engine)
    _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_fetch(_raw_posting("job-1"), _raw_posting("job-2")),
    )

    partial = _ingest(
        service,
        run_id=202,
        minute=2,
        fetch=_fetch(_raw_posting("job-1"), complete=False, status=200),
    )
    assert partial.source_fetch_status == "incomplete"
    assert partial.postings_seen == 1
    assert partial.postings_closed == 0
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                select(posting.c.closed_at).where(posting.c.external_id == "job-2")
            ).scalar_one()
            is None
        )

    not_modified = _ingest(
        service,
        run_id=203,
        minute=3,
        fetch=_fetch(complete=False, status=304),
    )
    assert not_modified.source_fetch_status == "incomplete"
    assert not_modified.postings_seen == 0
    assert not_modified.postings_updated == 0

    retained_non_200 = _ingest(
        service,
        run_id=207,
        minute=4,
        fetch=_fetch(
            _raw_posting("job-1", description="retained changed row"),
            complete=False,
            status=429,
        ),
    )
    assert retained_non_200.source_fetch_status == "incomplete"
    assert retained_non_200.postings_seen == 1
    assert retained_non_200.versions_created == 1
    assert retained_non_200.postings_closed == 0

    closed = _ingest(
        service,
        run_id=204,
        minute=5,
        fetch=_fetch(_raw_posting("job-1")),
    )
    assert closed.reconciliation_ran is True
    assert closed.postings_closed == 1

    future_close = BASE + timedelta(minutes=20)
    with ingestion_engine.begin() as conn:
        conn.execute(
            update(posting).where(posting.c.external_id == "job-2").values(closed_at=future_close)
        )
    too_early = _ingest(
        service,
        run_id=205,
        minute=6,
        fetch=_fetch(_raw_posting("job-2")),
    )
    assert too_early.postings_reopened == 0
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                select(posting.c.closed_at).where(posting.c.external_id == "job-2")
            ).scalar_one()
            == future_close
        )

    reopened = _ingest(
        service,
        run_id=206,
        minute=21,
        fetch=_fetch(_raw_posting("job-2")),
    )
    assert reopened.postings_reopened == 1
    assert reopened.postings_updated >= 1


def test_ordering_ineligible_attempt_persists_stale_without_mutation(ingestion_engine) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201, 202))
    service = IngestionService(ingestion_engine)
    first = _ingest(
        service,
        run_id=201,
        minute=10,
        fetch=_fetch(_raw_posting()),
    )
    stale = _ingest(
        service,
        run_id=202,
        minute=5,
        fetch=_fetch(_raw_posting(description="would change")),
    )
    assert stale.source_fetch_status == "incomplete"
    assert stale.reason_code == STALE_OBSERVATION
    assert stale.postings_seen == 1
    assert stale.versions_created == stale.postings_updated == 0
    with ingestion_engine.connect() as conn:
        assert len(list(conn.execute(select(posting_version)).all())) == 1
        assert (
            conn.execute(select(posting.c.current_version_id)).scalar_one()
            == first.created_posting_version_ids[0]
        )


class _MalformedObservation:
    def __init__(self, http_status, etag, last_modified) -> None:
        self.http_status = http_status
        self.etag = etag
        self.last_modified = last_modified


def test_invalid_observation_retains_only_independently_valid_facts(
    ingestion_engine,
) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201, 202))
    service = IngestionService(ingestion_engine)
    valid_status = _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_MalformedObservation(418, '"kept"', object()),
    )
    assert valid_status.source_fetch_status == "failed"
    assert valid_status.reason_code == INVALID_OBSERVATION
    assert valid_status.http_status == 418

    invalid_status = _ingest(
        service,
        run_id=202,
        minute=2,
        fetch=_MalformedObservation(True, object(), "kept-last-modified"),
    )
    assert invalid_status.http_status is None
    with ingestion_engine.connect() as conn:
        rows = list(conn.execute(select(source_fetch).order_by(source_fetch.c.id)).mappings())
        assert rows[0]["etag"] == '"kept"'
        assert rows[0]["last_modified"] is None
        assert rows[1]["etag"] is None
        assert rows[1]["last_modified"] == "kept-last-modified"
        assert len(list(conn.execute(select(posting)).all())) == 0


@pytest.mark.parametrize("field", ["source_published_at", "source_updated_at"])
def test_malformed_source_timezone_commits_only_a_redacted_failed_fetch(
    ingestion_engine,
    caplog,
    field: str,
) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201,))
    service = IngestionService(ingestion_engine)
    malformed_timestamp = datetime(2026, 7, 1, tzinfo=_MalformedTimezone())
    malformed_posting = replace(_raw_posting(), **{field: malformed_timestamp})

    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        result = _ingest(
            service,
            run_id=201,
            minute=1,
            fetch=_fetch(malformed_posting),
        )

    assert result.source_fetch_status == "failed"
    assert result.reason_code == INVALID_OBSERVATION
    assert result.postings_seen == 0
    assert result.source_fetch_postings_new == 0
    assert result.source_fetch_postings_changed == 0
    assert result.postings_created == 0
    assert result.postings_updated == 0
    assert result.versions_created == 0
    assert result.postings_closed == 0
    assert result.postings_reopened == 0
    assert result.created_posting_version_ids == ()
    with ingestion_engine.connect() as conn:
        fetch_rows = list(conn.execute(select(source_fetch)).mappings())
        assert len(fetch_rows) == 1
        assert fetch_rows[0]["status"] == "failed"
        assert fetch_rows[0]["postings_seen"] == 0
        assert fetch_rows[0]["postings_new"] == 0
        assert fetch_rows[0]["postings_changed"] == 0
        assert fetch_rows[0]["error"] == {"code": INVALID_OBSERVATION}
        assert len(list(conn.execute(select(posting)).all())) == 0
        assert len(list(conn.execute(select(posting_version)).all())) == 0
    surfaced = f"{result!r}\n{caplog.text}"
    assert MALFORMED_TIMEZONE_ERROR not in surfaced
    assert MALFORMED_TIMEZONE_SENTINEL not in surfaced


def test_every_transport_code_and_failed_key_replay(ingestion_engine) -> None:
    run_ids = tuple(range(201, 211))
    _seed_prerequisites(ingestion_engine, run_ids=run_ids)
    service = IngestionService(ingestion_engine)
    for minute, (run_id, code) in enumerate(zip(run_ids, TransportFailureCode, strict=True), 1):
        started = BASE + timedelta(minutes=minute)
        result = service.record_fetch_failure(
            run_id=run_id,
            source_endpoint_id=101,
            started_at=started,
            finished_at=started + timedelta(seconds=10),
            reason_code=code,
        )
        assert result.source_fetch_status == "failed"
        assert result.reason_code == code.value
        assert result.http_status is None
    replay = _ingest(
        service,
        run_id=201,
        minute=99,
        fetch=_fetch(_raw_posting()),
    )
    assert replay.replayed is True
    assert replay.reason_code == TransportFailureCode.CONNECT_TIMEOUT.value
    with ingestion_engine.connect() as conn:
        assert len(list(conn.execute(select(posting)).all())) == 0


def _seed_volume_history(engine, *, suspicious_previous: bool = False) -> None:
    _add_run(engine, 250)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO source_fetch
                    (run_id, endpoint_id, status, http_status, postings_seen,
                     postings_new, postings_changed, started_at, finished_at)
                VALUES
                    (250, 101, 'complete', 200, 50, 0, 0, :started, :finished)
                """
            ),
            {"started": BASE - timedelta(hours=2), "finished": BASE - timedelta(hours=1)},
        )
    if suspicious_previous:
        _add_run(engine, 251)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO source_fetch
                        (run_id, endpoint_id, status, http_status, postings_seen,
                         postings_new, postings_changed, error, started_at, finished_at)
                    VALUES
                        (251, 101, 'incomplete', 200, 0, 0, 0,
                         '{"code":"ingestion.suspicious_zero"}'::jsonb,
                         :started, :finished)
                    """
                ),
                {
                    "started": BASE - timedelta(minutes=40),
                    "finished": BASE - timedelta(minutes=30),
                },
            )


def test_classified_zeros_alert_suppression_and_nonzero_recovery(ingestion_engine) -> None:
    _seed_prerequisites(
        ingestion_engine,
        run_ids=(201, 202, 203, 204),
    )
    _seed_volume_history(ingestion_engine)
    service = IngestionService(ingestion_engine)
    _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_fetch(_raw_posting()),
    )

    first_zero = _ingest(service, run_id=202, minute=2, fetch=_fetch())
    assert first_zero.reason_code == SUSPICIOUS_ZERO
    assert first_zero.source_fetch_status == "incomplete"
    assert first_zero.postings_closed == 0
    assert first_zero.alert_required is False

    second_zero = _ingest(service, run_id=203, minute=3, fetch=_fetch())
    assert second_zero.endpoint_marked_failing is True
    assert second_zero.alert_required is True
    assert second_zero.alert_code == SUSPICIOUS_ZERO
    assert second_zero.postings_closed == 0

    _add_run(ingestion_engine, 205)
    third_zero = _ingest(service, run_id=205, minute=4, fetch=_fetch())
    assert third_zero.endpoint_marked_failing is False
    assert third_zero.alert_required is False

    recovery = _ingest(
        service,
        run_id=204,
        minute=5,
        fetch=_fetch(_raw_posting()),
    )
    assert recovery.endpoint_marked_active is True
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                select(source_endpoint.c.status).where(source_endpoint.c.id == 101)
            ).scalar_one()
            == "active"
        )

    replay = _ingest(service, run_id=203, minute=99, fetch=object())
    assert replay.replayed is True
    assert replay.endpoint_marked_failing is replay.endpoint_marked_active is False
    assert replay.alert_required is False


def test_unclassified_complete_zero_closes_all_eligible_open_rows(ingestion_engine) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201, 202))
    service = IngestionService(ingestion_engine)
    _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_fetch(_raw_posting("one"), _raw_posting("two")),
    )
    zero = _ingest(service, run_id=202, minute=2, fetch=_fetch())
    assert zero.source_fetch_status == "complete"
    assert zero.reconciliation_ran is True
    assert zero.postings_closed == 2


def test_endpoint_local_identity_allows_same_external_id(ingestion_engine) -> None:
    _seed_prerequisites(
        ingestion_engine,
        endpoint_ids=(101, 102),
        run_ids=(201, 202),
    )
    service = IngestionService(ingestion_engine)
    _ingest(
        service,
        run_id=201,
        endpoint_id=101,
        minute=1,
        fetch=_fetch(_raw_posting("same-id")),
    )
    _ingest(
        service,
        run_id=202,
        endpoint_id=102,
        minute=1,
        fetch=_fetch(_raw_posting("same-id")),
    )
    with ingestion_engine.connect() as conn:
        rows = list(
            conn.execute(
                select(posting.c.source_endpoint_id, posting.c.external_id).order_by(
                    posting.c.source_endpoint_id
                )
            )
        )
    assert rows == [(101, "same-id"), (102, "same-id")]


def test_independent_endpoints_do_not_share_an_ingestion_lock(ingestion_engine) -> None:
    _seed_prerequisites(
        ingestion_engine,
        endpoint_ids=(101, 102),
        run_ids=(201, 202),
    )
    first_mutated = threading.Event()
    release_first = threading.Event()
    errors = []
    results = []

    def hold_first(stage: str, _conn) -> None:
        if stage == "posting" and not first_mutated.is_set():
            first_mutated.set()
            assert release_first.wait(timeout=10)

    first_service = IngestionService(ingestion_engine, mutation_hook=hold_first)
    second_service = IngestionService(ingestion_engine)

    def first() -> None:
        try:
            results.append(
                _ingest(
                    first_service,
                    run_id=201,
                    endpoint_id=101,
                    minute=1,
                    fetch=_fetch(_raw_posting("endpoint-one")),
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in assertion
            errors.append(exc)

    first_thread = threading.Thread(target=first)
    first_thread.start()
    assert first_mutated.wait(timeout=10)

    second_thread = threading.Thread(
        target=lambda: results.append(
            _ingest(
                second_service,
                run_id=202,
                endpoint_id=102,
                minute=1,
                fetch=_fetch(_raw_posting("endpoint-two")),
            )
        )
    )
    second_thread.start()
    second_thread.join(timeout=10)
    assert not second_thread.is_alive(), "independent endpoint was blocked by the first"
    release_first.set()
    first_thread.join(timeout=10)
    assert not first_thread.is_alive()
    assert errors == []
    assert len(results) == 2
    assert all(result.source_fetch_status == "complete" for result in results)


def test_same_key_concurrency_has_one_first_execution_and_one_replay(
    ingestion_engine,
) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201,))
    service = IngestionService(ingestion_engine)
    barrier = threading.Barrier(2)
    original = service._initial_replay

    def synchronized_initial(self, **kwargs):
        result = original(**kwargs)
        barrier.wait(timeout=10)
        return result

    service._initial_replay = MethodType(synchronized_initial, service)  # type: ignore[method-assign]
    results = []
    errors = []

    def run() -> None:
        try:
            results.append(
                _ingest(
                    service,
                    run_id=201,
                    minute=1,
                    fetch=_fetch(_raw_posting()),
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in assertion
            errors.append(exc)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert errors == []
    assert [result.replayed for result in results].count(False) == 1
    assert [result.replayed for result in results].count(True) == 1
    first_result = next(result for result in results if not result.replayed)
    replay_result = next(result for result in results if result.replayed)
    assert replay_result.versions_created == 0
    assert replay_result.created_posting_version_ids == first_result.created_posting_version_ids
    with ingestion_engine.connect() as conn:
        assert len(list(conn.execute(select(source_fetch)).all())) == 1
        assert len(list(conn.execute(select(posting)).all())) == 1
        assert len(list(conn.execute(select(posting_version)).all())) == 1


def test_different_run_same_endpoint_concurrency_serializes_and_marks_stale(
    ingestion_engine,
) -> None:
    _seed_prerequisites(ingestion_engine, run_ids=(201, 202))
    service = IngestionService(ingestion_engine)
    barrier = threading.Barrier(2)
    original = service._initial_replay

    def synchronized_initial(self, **kwargs):
        result = original(**kwargs)
        barrier.wait(timeout=10)
        return result

    service._initial_replay = MethodType(synchronized_initial, service)  # type: ignore[method-assign]
    results = []

    def run(run_id: int, description: str) -> None:
        results.append(
            _ingest(
                service,
                run_id=run_id,
                minute=1,
                fetch=_fetch(_raw_posting(description=description)),
            )
        )

    threads = [
        threading.Thread(target=run, args=(201, "version a")),
        threading.Thread(target=run, args=(202, "version b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert sorted(result.reason_code or "" for result in results) == ["", STALE_OBSERVATION]
    assert sorted(result.source_fetch_status for result in results) == ["complete", "incomplete"]
    with ingestion_engine.connect() as conn:
        assert len(list(conn.execute(select(posting_version)).all())) == 1


def _seed_direct_posting(engine, *, closed_at=None) -> None:
    _seed_prerequisites(engine, run_ids=(201, 202))
    service = IngestionService(engine)
    _ingest(
        service,
        run_id=201,
        minute=1,
        fetch=_fetch(_raw_posting()),
    )
    if closed_at is not None:
        with engine.begin() as conn:
            conn.execute(update(posting).values(closed_at=closed_at))


@pytest.mark.parametrize(
    "stage",
    ["posting", "version", "close", "reopen", "endpoint_status", "source_fetch"],
)
def test_database_failure_at_each_mutation_stage_rolls_back(
    ingestion_engine,
    stage: str,
) -> None:
    if stage in {"close", "reopen"}:
        _seed_direct_posting(
            ingestion_engine,
            closed_at=(BASE + timedelta(minutes=2)) if stage == "reopen" else None,
        )
        run_id = 202
        fetch = _fetch(_raw_posting()) if stage == "reopen" else _fetch()
        minute = 3
    elif stage == "endpoint_status":
        _seed_prerequisites(
            ingestion_engine,
            run_ids=(201,),
        )
        _seed_volume_history(ingestion_engine, suspicious_previous=True)
        run_id = 201
        fetch = _fetch()
        minute = 1
    else:
        _seed_prerequisites(ingestion_engine, run_ids=(201,))
        run_id = 201
        fetch = _fetch(_raw_posting())
        minute = 1

    with ingestion_engine.connect() as conn:
        before = {
            "fetches": len(list(conn.execute(select(source_fetch)).all())),
            "postings": [dict(row) for row in conn.execute(select(posting)).mappings()],
            "versions": len(list(conn.execute(select(posting_version)).all())),
            "status": conn.execute(
                select(source_endpoint.c.status).where(source_endpoint.c.id == 101)
            ).scalar_one(),
        }

    def fail_at(requested: str, _conn) -> None:
        if requested == stage:
            raise OperationalError("mutation", {}, Exception("synthetic"))

    service = IngestionService(ingestion_engine, mutation_hook=fail_at)
    with pytest.raises(IngestionDatabaseError):
        _ingest(service, run_id=run_id, minute=minute, fetch=fetch)

    with ingestion_engine.connect() as conn:
        after = {
            "fetches": len(list(conn.execute(select(source_fetch)).all())),
            "postings": [dict(row) for row in conn.execute(select(posting)).mappings()],
            "versions": len(list(conn.execute(select(posting_version)).all())),
            "status": conn.execute(
                select(source_endpoint.c.status).where(source_endpoint.c.id == 101)
            ).scalar_one(),
        }
    assert after == before
