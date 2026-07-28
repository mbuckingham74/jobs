"""Network-free tests for service ordering, replay, and closed inputs."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import pytest

from app.ingestion.results import (
    IngestionInputError,
    IngestionResult,
    TransportFailureCode,
)
from app.ingestion.service import IngestionService

START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
FINISH = datetime(2026, 7, 1, 12, 1, tzinfo=UTC)


class _MalformedTimezone(tzinfo):
    def utcoffset(self, value: datetime | None):
        raise RuntimeError("sensitive-call-metadata-timezone-sentinel")


class _UntouchableEngine:
    def __getattr__(self, name: str):
        pytest.fail(f"engine must not be touched during callable validation: {name}")


def _replay() -> IngestionResult:
    return IngestionResult(
        source_fetch_id=9,
        run_id=1,
        source_endpoint_id=2,
        source_fetch_status="failed",
        http_status=None,
        postings_seen=0,
        source_fetch_postings_new=0,
        source_fetch_postings_changed=0,
        reason_code="adapter.connect_error",
        postings_created=0,
        postings_updated=0,
        versions_created=0,
        postings_closed=0,
        postings_reopened=0,
        created_posting_version_ids=(),
        reconciliation_ran=False,
        replayed=True,
        endpoint_marked_failing=False,
        endpoint_marked_active=False,
        alert_required=False,
        alert_code=None,
    )


def test_malformed_existing_key_returns_replay_without_inspection(monkeypatch) -> None:
    service = IngestionService(object())  # type: ignore[arg-type]
    replay = _replay()
    monkeypatch.setattr(service, "_initial_replay", lambda **kwargs: replay)
    monkeypatch.setattr(
        service,
        "_verify_targets",
        lambda **kwargs: pytest.fail("targets must not be inspected on replay"),
    )
    assert (
        service.ingest_fetch_result(
            run_id=1,
            source_endpoint_id=2,
            fetch_result=object(),
            started_at=START,
            finished_at=FINISH,
        )
        is replay
    )


def test_malformed_missing_key_prepares_invalid_fetch(monkeypatch) -> None:
    service = IngestionService(object())  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_initial_replay", lambda **kwargs: None)
    monkeypatch.setattr(service, "_verify_targets", lambda **kwargs: None)
    captured = {}

    def fake_write(**kwargs):
        captured.update(kwargs)
        return _replay()

    monkeypatch.setattr(service, "_write_fetch", fake_write)
    service.ingest_fetch_result(
        run_id=1,
        source_endpoint_id=2,
        fetch_result=object(),
        started_at=START,
        finished_at=FINISH,
    )
    assert captured["prepared"] is None
    assert captured["invalid_facts"] == (None, None, None)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("run_id", True, "ingestion.invalid_run_id"),
        ("run_id", 0, "ingestion.invalid_run_id"),
        ("source_endpoint_id", False, "ingestion.invalid_source_endpoint_id"),
        ("started_at", datetime(2026, 7, 1), "ingestion.invalid_started_at"),
        ("finished_at", datetime(2026, 7, 1), "ingestion.invalid_finished_at"),
        (
            "finished_at",
            datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
            "ingestion.invalid_timestamp_order",
        ),
    ],
)
def test_minimal_callable_validation(field: str, value: object, code: str) -> None:
    service = IngestionService(object())  # type: ignore[arg-type]
    kwargs = {
        "run_id": 1,
        "source_endpoint_id": 2,
        "fetch_result": object(),
        "started_at": START,
        "finished_at": FINISH,
    }
    kwargs[field] = value
    with pytest.raises(IngestionInputError, match=code):
        service.ingest_fetch_result(**kwargs)


@pytest.mark.parametrize(
    ("method_name", "field", "code"),
    [
        ("ingest_fetch_result", "started_at", "ingestion.invalid_started_at"),
        ("ingest_fetch_result", "finished_at", "ingestion.invalid_finished_at"),
        ("record_fetch_failure", "started_at", "ingestion.invalid_started_at"),
        ("record_fetch_failure", "finished_at", "ingestion.invalid_finished_at"),
    ],
)
def test_malformed_callable_timezone_is_rejected_before_database_access(
    method_name: str,
    field: str,
    code: str,
) -> None:
    service = IngestionService(_UntouchableEngine())  # type: ignore[arg-type]
    kwargs = {
        "run_id": 1,
        "source_endpoint_id": 2,
        "started_at": START,
        "finished_at": FINISH,
    }
    if method_name == "ingest_fetch_result":
        kwargs["fetch_result"] = object()
    else:
        kwargs["reason_code"] = TransportFailureCode.CONNECT_ERROR
    kwargs[field] = datetime(2026, 7, 1, tzinfo=_MalformedTimezone())

    with pytest.raises(IngestionInputError) as exc_info:
        getattr(service, method_name)(**kwargs)

    assert exc_info.value.code == code


def test_transport_code_is_closed_and_checked_only_after_replay(monkeypatch) -> None:
    service = IngestionService(object())  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_initial_replay", lambda **kwargs: None)
    monkeypatch.setattr(service, "_verify_targets", lambda **kwargs: None)
    with pytest.raises(IngestionInputError, match="invalid_transport_failure_code"):
        service.record_fetch_failure(
            run_id=1,
            source_endpoint_id=2,
            started_at=START,
            finished_at=FINISH,
            reason_code="adapter.connect_error",
        )

    replay = _replay()
    monkeypatch.setattr(service, "_initial_replay", lambda **kwargs: replay)
    assert (
        service.record_fetch_failure(
            run_id=1,
            source_endpoint_id=2,
            started_at=START,
            finished_at=FINISH,
            reason_code=object(),
        )
        is replay
    )


def test_transport_failure_enum_is_exact() -> None:
    assert {item.value for item in TransportFailureCode} == {
        "adapter.connect_timeout",
        "adapter.read_timeout",
        "adapter.write_timeout",
        "adapter.pool_timeout",
        "adapter.connect_error",
        "adapter.read_error",
        "adapter.write_error",
        "adapter.protocol_error",
        "adapter.tls_error",
        "adapter.transport_error",
    }
