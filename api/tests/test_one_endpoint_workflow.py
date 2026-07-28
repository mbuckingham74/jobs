"""Deterministic closed-boundary tests for the Task 007 runner."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from app.ingestion import IngestionResult, TransportFailureCode
from app.sources.ats.contracts import SourceEndpoint
from app.workflow.registry import AdapterRegistry
from app.workflow.repository import EndpointSnapshot, reserved_counts
from app.workflow.results import (
    IngestionReasonCode,
    RunnerFailureCode,
    RunnerInputError,
    RunnerTargetError,
)
from app.workflow.service import (
    _TRANSPORT_CATEGORIES,
    _clock_now,
    _closed_failure,
    _counts_shape,
    _eligibility,
    _matrix,
    _registry_valid,
    _source_endpoint,
    _utc,
    _validate_inputs,
)
from app.workflow.validators import reconstruct_validators


def _ingestion(**overrides) -> IngestionResult:
    values = {
        "source_fetch_id": 10,
        "run_id": 20,
        "source_endpoint_id": 30,
        "source_fetch_status": "complete",
        "http_status": 200,
        "postings_seen": 1,
        "source_fetch_postings_new": 1,
        "source_fetch_postings_changed": 0,
        "reason_code": None,
        "postings_created": 1,
        "postings_updated": 0,
        "versions_created": 1,
        "postings_closed": 0,
        "postings_reopened": 0,
        "created_posting_version_ids": (40,),
        "reconciliation_ran": True,
        "replayed": False,
        "endpoint_marked_failing": False,
        "endpoint_marked_active": False,
        "alert_required": False,
        "alert_code": None,
    }
    values.update(overrides)
    return IngestionResult(**values)


def test_runner_failure_inventory_is_exact() -> None:
    assert len(RunnerFailureCode) == 28
    assert {item.value for item in RunnerFailureCode} == {
        "runner.invalid_endpoint_id",
        "runner.invalid_run_id",
        "runner.invalid_config_version",
        "runner.invalid_clock",
        "runner.pipeline_run_not_found",
        "runner.source_endpoint_not_found",
        "runner.pipeline_run_incompatible",
        "runner.database_error",
        "runner.endpoint_paused",
        "runner.endpoint_retired",
        "runner.adapter_deferred",
        "runner.adapter_not_implemented",
        "runner.adapter_unsupported",
        "runner.endpoint_malformed",
        "runner.endpoint_policy",
        "runner.endpoint_deleted",
        "runner.endpoint_changed",
        "runner.adapter_registry_defect",
        "runner.adapter_defect",
        "runner.clock_invalid",
        "runner.ingestion_database_error",
        "runner.ingestion_input_error",
        "runner.ingestion_target_error",
        "runner.ingestion_result_invalid",
        "runner.fetch_incomplete",
        "runner.cancelled",
        "runner.finalization_database_error",
        "runner.finalization_conflict",
    }
    assert {item.value for item in IngestionReasonCode} == {
        "ingestion.invalid_observation",
        "ingestion.suspicious_zero",
        "ingestion.stale_observation",
    }


def test_exact_complete_adapter_transport_category_mapping() -> None:
    assert _TRANSPORT_CATEGORIES == {
        "greenhouse.transport.connect_timeout": TransportFailureCode.CONNECT_TIMEOUT,
        "greenhouse.transport.read_timeout": TransportFailureCode.READ_TIMEOUT,
        "greenhouse.transport.connect_error": TransportFailureCode.CONNECT_ERROR,
        "greenhouse.transport.protocol_error": TransportFailureCode.PROTOCOL_ERROR,
        "greenhouse.transport_error": TransportFailureCode.TRANSPORT_ERROR,
        "lever.transport.connect_timeout": TransportFailureCode.CONNECT_TIMEOUT,
        "lever.transport.read_timeout": TransportFailureCode.READ_TIMEOUT,
        "lever.transport.write_timeout": TransportFailureCode.WRITE_TIMEOUT,
        "lever.transport.pool_timeout": TransportFailureCode.POOL_TIMEOUT,
        "lever.transport.connect_error": TransportFailureCode.CONNECT_ERROR,
        "lever.transport.read_error": TransportFailureCode.READ_ERROR,
        "lever.transport.write_error": TransportFailureCode.WRITE_ERROR,
        "lever.transport.protocol_error": TransportFailureCode.PROTOCOL_ERROR,
        "lever.transport_error": TransportFailureCode.TRANSPORT_ERROR,
    }
    assert "adapter.tls_error" not in {value.value for value in _TRANSPORT_CATEGORIES.values()}


@pytest.mark.parametrize(
    ("endpoint_id", "run_id", "config", "code"),
    [
        (True, None, "v1", RunnerFailureCode.INVALID_ENDPOINT_ID),
        (1, False, "v1", RunnerFailureCode.INVALID_RUN_ID),
        (1, None, "", RunnerFailureCode.INVALID_CONFIG_VERSION),
        (1, None, " space", RunnerFailureCode.INVALID_CONFIG_VERSION),
        (1, None, "a" * 129, RunnerFailureCode.INVALID_CONFIG_VERSION),
    ],
)
def test_input_projection_is_closed(endpoint_id, run_id, config, code) -> None:
    with pytest.raises(RunnerInputError) as caught:
        _validate_inputs(endpoint_id, run_id, config)
    assert caught.value.code is code
    assert str(caught.value) == code.value


@pytest.mark.parametrize(
    ("endpoint_id", "run_id", "config"),
    [
        (1, None, "a"),
        (2**63 - 1, 2**63 - 1, "A0._-" + ("z" * 123)),
    ],
)
def test_valid_input_boundaries_are_preserved(endpoint_id, run_id, config) -> None:
    assert _validate_inputs(endpoint_id, run_id, config) == (endpoint_id, run_id, config)


@pytest.mark.parametrize(
    ("endpoint_id", "run_id", "config", "code"),
    [
        (0, None, "v1", RunnerFailureCode.INVALID_ENDPOINT_ID),
        (-1, None, "v1", RunnerFailureCode.INVALID_ENDPOINT_ID),
        (1.0, None, "v1", RunnerFailureCode.INVALID_ENDPOINT_ID),
        ("1", None, "v1", RunnerFailureCode.INVALID_ENDPOINT_ID),
        (1, 0, "v1", RunnerFailureCode.INVALID_RUN_ID),
        (1, -1, "v1", RunnerFailureCode.INVALID_RUN_ID),
        (1, 1.0, "v1", RunnerFailureCode.INVALID_RUN_ID),
        (1, None, None, RunnerFailureCode.INVALID_CONFIG_VERSION),
        (1, None, "v 1", RunnerFailureCode.INVALID_CONFIG_VERSION),
        (1, None, "v1/", RunnerFailureCode.INVALID_CONFIG_VERSION),
    ],
)
def test_additional_invalid_input_boundaries(endpoint_id, run_id, config, code) -> None:
    with pytest.raises(RunnerInputError) as caught:
        _validate_inputs(endpoint_id, run_id, config)
    assert caught.value.code is code


def test_reserved_counts_are_exact_and_reject_boolean_integers() -> None:
    counts = reserved_counts(3, "greenhouse")
    assert list(counts) == [
        "schema_version",
        "endpoint_id",
        "adapter_kind",
        "attempt_recorded",
        "source_fetch_id",
        "http_status",
        "postings_seen",
        "postings_new",
        "postings_changed",
    ]
    assert _counts_shape(counts, endpoint_id=3, adapter_kind="greenhouse") == "reserved"
    malformed = dict(counts, postings_seen=False)
    assert _counts_shape(malformed, endpoint_id=3, adapter_kind="greenhouse") is None
    assert _counts_shape(dict(counts, extra=1), endpoint_id=3, adapter_kind="greenhouse") is None


def test_attempt_counts_shape_requires_exact_json_types() -> None:
    counts = {
        "schema_version": 1,
        "endpoint_id": 3,
        "adapter_kind": "lever",
        "attempt_recorded": True,
        "source_fetch_id": 8,
        "http_status": 304,
        "postings_seen": 0,
        "postings_new": 0,
        "postings_changed": 0,
    }
    assert _counts_shape(counts, endpoint_id=3, adapter_kind="lever") == "attempt"
    for key in ("schema_version", "endpoint_id", "source_fetch_id", "http_status"):
        assert (
            _counts_shape(
                dict(counts, **{key: True}),
                endpoint_id=3,
                adapter_kind="lever",
            )
            is None
        )
    assert (
        _counts_shape(
            dict(counts, postings_changed=-1),
            endpoint_id=3,
            adapter_kind="lever",
        )
        is None
    )


def _snapshot(**overrides) -> EndpointSnapshot:
    values = {
        "id": 3,
        "company_id": 4,
        "status": "active",
        "kind": "greenhouse",
        "token": "invented-token",
        "region": "global",
        "base_url": None,
    }
    values.update(overrides)
    return EndpointSnapshot(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),
        ({"status": "failing"}, None),
        ({"status": "paused"}, RunnerFailureCode.ENDPOINT_PAUSED),
        ({"status": "retired"}, RunnerFailureCode.ENDPOINT_RETIRED),
        ({"kind": "ashby"}, RunnerFailureCode.ADAPTER_DEFERRED),
        ({"kind": "workday"}, RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ({"kind": "workable"}, RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ({"kind": "smartrecruiters"}, RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ({"kind": "recruitee"}, RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ({"kind": "custom"}, RunnerFailureCode.ADAPTER_UNSUPPORTED),
        ({"kind": "unknown-sensitive-kind"}, RunnerFailureCode.ENDPOINT_MALFORMED),
        ({"company_id": True}, RunnerFailureCode.ENDPOINT_MALFORMED),
        ({"status": "unknown"}, RunnerFailureCode.ENDPOINT_MALFORMED),
        ({"token": object()}, RunnerFailureCode.ENDPOINT_MALFORMED),
        ({"region": None}, RunnerFailureCode.ENDPOINT_MALFORMED),
        ({"base_url": object()}, RunnerFailureCode.ENDPOINT_MALFORMED),
    ],
)
def test_every_endpoint_eligibility_and_malformed_branch(overrides, expected) -> None:
    assert _eligibility(_snapshot(**overrides)) is expected


def test_frozen_source_endpoint_construction_uses_exact_four_fields() -> None:
    snapshot = _snapshot(
        kind="lever",
        token="invented-source-token",
        region="eu",
        base_url="https://invented.invalid",
    )
    assert _source_endpoint(snapshot) == SourceEndpoint(
        kind="lever",
        token="invented-source-token",
        region="eu",
        base_url="https://invented.invalid",
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.token = "changed"  # type: ignore[misc]


def test_registry_shape_is_exact_without_calling_factories() -> None:
    calls = []

    def factory():
        calls.append(1)
        return object()

    valid = AdapterRegistry(greenhouse=factory, lever=factory)  # type: ignore[arg-type]
    assert _registry_valid(valid) is True
    assert calls == []
    assert _registry_valid(object()) is False
    assert (
        _registry_valid(AdapterRegistry(greenhouse=None, lever=factory))  # type: ignore[arg-type]
        is False
    )


class _MalformedTimezone(tzinfo):
    def utcoffset(self, dt):
        raise RuntimeError("private-clock-sentinel")

    def dst(self, dt):
        return None


def test_clock_accepts_only_aware_zero_offset_and_redacts_tzinfo_failures() -> None:
    value = datetime(2026, 7, 1, tzinfo=UTC)
    assert _utc(value) == value
    for invalid in (
        datetime(2026, 7, 1),
        datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(2026, 7, 1, tzinfo=_MalformedTimezone()),
    ):
        with pytest.raises(ValueError) as caught:
            _utc(invalid)
        assert "private-clock-sentinel" not in str(caught.value)

    class RaisingClock:
        def now(self):
            raise RuntimeError("private-clock-sentinel")

    with pytest.raises(ValueError) as caught:
        _clock_now(RaisingClock())
    assert "private-clock-sentinel" not in str(caught.value)


def test_per_field_validator_clearing_preservation_and_tied_order() -> None:
    rows = [
        {
            "id": 9,
            "status": "incomplete",
            "http_status": 304,
            "error": None,
            "etag": None,
            "last_modified": "new-last-modified",
        },
        {
            "id": 8,
            "status": "complete",
            "http_status": 200,
            "error": None,
            "etag": None,
            "last_modified": None,
        },
        {
            "id": 7,
            "status": "complete",
            "http_status": 200,
            "error": None,
            "etag": "old-etag",
            "last_modified": "old-last-modified",
        },
    ]
    assert reconstruct_validators(rows).etag is None
    assert reconstruct_validators(rows).last_modified == "new-last-modified"


def test_validator_reconstruction_excludes_unsafe_rows() -> None:
    unsafe = [
        {
            "status": "failed",
            "http_status": None,
            "error": {"code": "adapter.connect_error"},
            "etag": "secret",
            "last_modified": "secret",
        },
        {
            "status": "incomplete",
            "http_status": 200,
            "error": None,
            "etag": "secret",
            "last_modified": "secret",
        },
        {
            "status": "incomplete",
            "http_status": 304,
            "error": None,
            "etag": "safe-etag",
            "last_modified": None,
        },
    ]
    assert reconstruct_validators(unsafe).etag == "safe-etag"
    assert reconstruct_validators(unsafe).last_modified is None


@pytest.mark.parametrize(
    ("result", "status", "failure"),
    [
        (_ingestion(), "succeeded", None),
        (
            _ingestion(
                source_fetch_status="incomplete",
                http_status=304,
                postings_seen=0,
            ),
            "succeeded",
            None,
        ),
        (
            _ingestion(
                source_fetch_status="incomplete",
                postings_seen=2,
                reason_code=None,
            ),
            "partial",
            RunnerFailureCode.FETCH_INCOMPLETE,
        ),
        (
            _ingestion(
                source_fetch_status="incomplete",
                postings_seen=0,
                reason_code=IngestionReasonCode.SUSPICIOUS_ZERO.value,
            ),
            "partial",
            IngestionReasonCode.SUSPICIOUS_ZERO,
        ),
        (
            _ingestion(
                source_fetch_status="failed",
                http_status=None,
                postings_seen=0,
                reason_code=TransportFailureCode.CONNECT_TIMEOUT.value,
            ),
            "failed",
            TransportFailureCode.CONNECT_TIMEOUT,
        ),
        (
            _ingestion(
                source_fetch_status="incomplete",
                postings_seen=0,
                reason_code=IngestionReasonCode.INVALID_OBSERVATION.value,
            ),
            "failed",
            IngestionReasonCode.INVALID_OBSERVATION,
        ),
        (
            _ingestion(
                source_fetch_status="failed",
                postings_seen=0,
                reason_code="private-arbitrary-reason",
            ),
            "failed",
            RunnerFailureCode.INGESTION_RESULT_INVALID,
        ),
    ],
)
def test_exact_terminal_matrix(result, status, failure) -> None:
    assert _matrix(result) == (status, failure)


@pytest.mark.parametrize(
    ("result", "status", "failure"),
    [
        (_ingestion(postings_seen=0), "succeeded", None),
        (
            _ingestion(
                source_fetch_status="incomplete",
                http_status=200,
                postings_seen=1,
                reason_code=IngestionReasonCode.STALE_OBSERVATION.value,
            ),
            "partial",
            IngestionReasonCode.STALE_OBSERVATION,
        ),
        (
            _ingestion(
                source_fetch_status="incomplete",
                http_status=200,
                postings_seen=0,
                reason_code=None,
            ),
            "failed",
            RunnerFailureCode.FETCH_INCOMPLETE,
        ),
        (
            _ingestion(
                source_fetch_status="failed",
                http_status=None,
                postings_seen=0,
                reason_code=IngestionReasonCode.INVALID_OBSERVATION.value,
            ),
            "failed",
            IngestionReasonCode.INVALID_OBSERVATION,
        ),
        (
            _ingestion(
                source_fetch_status="complete",
                reason_code="unknown-sensitive-reason",
            ),
            "failed",
            RunnerFailureCode.INGESTION_RESULT_INVALID,
        ),
        (
            _ingestion(
                source_fetch_status="unknown",
                reason_code=None,
            ),
            "failed",
            RunnerFailureCode.INGESTION_RESULT_INVALID,
        ),
    ],
)
def test_remaining_terminal_matrix_rows(result, status, failure) -> None:
    assert _matrix(result) == (status, failure)


def test_unknown_persisted_failure_is_rejected_as_incompatible() -> None:
    with pytest.raises(RunnerTargetError) as caught:
        _closed_failure("unknown-sensitive-reason")
    assert caught.value.code is RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE
    assert "unknown-sensitive-reason" not in str(caught.value)


def test_adapter_registry_is_frozen_and_exactly_two_factories() -> None:
    registry = AdapterRegistry(greenhouse=lambda: object(), lever=lambda: object())  # type: ignore[arg-type]
    assert registry.greenhouse is not None
    assert registry.lever is not None
    with pytest.raises(AttributeError):
        registry.greenhouse = lambda: object()  # type: ignore[misc,assignment]


def test_datetime_fixture_is_aware_zero_offset() -> None:
    assert datetime(2026, 7, 1, tzinfo=UTC).utcoffset().total_seconds() == 0
