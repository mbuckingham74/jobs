"""One persisted endpoint to one Task 006 ingestion attempt."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.ingestion import (
    IngestionDatabaseError,
    IngestionInputError,
    IngestionResult,
    IngestionService,
    IngestionTargetError,
    TransportFailureCode,
)
from app.sources.ats.contracts import ConditionalHeaders, FetchResult, SourceEndpoint
from app.sources.ats.greenhouse import GreenhouseEndpointError, GreenhouseTransportError
from app.sources.ats.lever import LeverEndpointError, LeverTransportError
from app.workflow.registry import AdapterRegistry, UTCClock
from app.workflow.repository import (
    EndpointSnapshot,
    attempt_counts,
    create_run,
    finish_run,
    lock_endpoint,
    lock_run,
    matching_fetch_row,
    reserved_counts,
    validator_rows,
)
from app.workflow.results import (
    IngestionReasonCode,
    OneEndpointFailureCode,
    OneEndpointRunnerResult,
    RunnerDatabaseError,
    RunnerFailureCode,
    RunnerInputError,
    RunnerTargetError,
)
from app.workflow.validators import reconstruct_validators

logger = logging.getLogger("app.workflow.one_endpoint")

_CONFIG_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KINDS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "workable",
    "smartrecruiters",
    "recruitee",
    "custom",
}
_STATUSES = {"active", "paused", "failing", "retired"}
_UNIMPLEMENTED = {"workday", "workable", "smartrecruiters", "recruitee"}
_TERMINAL_STATUSES = {"succeeded", "partial", "failed"}
_COUNT_KEYS = {
    "schema_version",
    "endpoint_id",
    "adapter_kind",
    "attempt_recorded",
    "source_fetch_id",
    "http_status",
    "postings_seen",
    "postings_new",
    "postings_changed",
}
_TRANSPORT_CATEGORIES = {
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


@dataclass(frozen=True, slots=True)
class _Bound:
    run: dict[str, Any]
    endpoint_id: int
    adapter_kind: str
    endpoint: EndpointSnapshot | None
    conditional: ConditionalHeaders | None


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError
    try:
        offset = value.utcoffset()
    except Exception:
        raise ValueError from None
    if offset != timedelta(0):
        raise ValueError
    try:
        return value.astimezone(UTC)
    except Exception:
        raise ValueError from None


def _clock_now(clock: UTCClock) -> datetime:
    try:
        value = clock.now()
    except Exception:
        raise ValueError from None
    return _utc(value)


def _validate_inputs(
    source_endpoint_id: object,
    run_id: object,
    config_version: object,
) -> tuple[int, int | None, str]:
    if type(source_endpoint_id) is not int or source_endpoint_id <= 0:  # noqa: E721
        raise RunnerInputError(RunnerFailureCode.INVALID_ENDPOINT_ID)
    if run_id is not None and (type(run_id) is not int or run_id <= 0):  # noqa: E721
        raise RunnerInputError(RunnerFailureCode.INVALID_RUN_ID)
    if not isinstance(config_version, str) or _CONFIG_VERSION.fullmatch(config_version) is None:
        raise RunnerInputError(RunnerFailureCode.INVALID_CONFIG_VERSION)
    return source_endpoint_id, run_id, config_version


def _snapshot_is_well_formed(snapshot: EndpointSnapshot) -> bool:
    return (
        type(snapshot.company_id) is int
        and snapshot.company_id > 0
        and isinstance(snapshot.status, str)
        and snapshot.status in _STATUSES
        and isinstance(snapshot.kind, str)
        and snapshot.kind in _KINDS
        and isinstance(snapshot.token, str)
        and isinstance(snapshot.region, str)
        and (snapshot.base_url is None or isinstance(snapshot.base_url, str))
    )


def _kind(snapshot: EndpointSnapshot) -> str:
    return (
        snapshot.kind if isinstance(snapshot.kind, str) and snapshot.kind in _KINDS else "malformed"
    )


def _counts_shape(
    value: object,
    *,
    endpoint_id: int,
    adapter_kind: str,
) -> str | None:
    if type(value) is not dict or set(value) != _COUNT_KEYS:  # noqa: E721
        return None
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:  # noqa: E721
        return None
    if type(value["endpoint_id"]) is not int or value["endpoint_id"] != endpoint_id:  # noqa: E721
        return None
    if type(value["adapter_kind"]) is not str or value["adapter_kind"] != adapter_kind:  # noqa: E721
        return None
    if type(value["attempt_recorded"]) is not bool:  # noqa: E721
        return None
    for key in ("postings_seen", "postings_new", "postings_changed"):
        if type(value[key]) is not int or value[key] < 0:  # noqa: E721
            return None
    if value["attempt_recorded"] is False:
        if (
            value["source_fetch_id"] is not None
            or value["http_status"] is not None
            or any(value[key] != 0 for key in ("postings_seen", "postings_new", "postings_changed"))
        ):
            return None
        return "reserved"
    if type(value["source_fetch_id"]) is not int or value["source_fetch_id"] <= 0:  # noqa: E721
        return None
    if value["http_status"] is not None and (
        type(value["http_status"]) is not int  # noqa: E721
        or not 100 <= value["http_status"] <= 599
    ):
        return None
    return "attempt"


def _error_code(error: object) -> str | None:
    if error is None:
        return None
    if type(error) is not dict or set(error) != {"code"}:  # noqa: E721
        return ""
    code = error.get("code")
    return code if type(code) is str else ""  # noqa: E721


def _validate_owned_run(
    row: dict[str, Any],
    *,
    endpoint_id: int,
    adapter_kind: str,
    config_version: str,
) -> str:
    try:
        _utc(row.get("started_at"))
        if row.get("finished_at") is not None:
            finished = _utc(row["finished_at"])
            if finished < _utc(row["started_at"]):
                raise ValueError
    except ValueError:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE) from None
    shape = _counts_shape(row.get("counts"), endpoint_id=endpoint_id, adapter_kind=adapter_kind)
    valid = (
        row.get("run_kind") == "manual"
        and row.get("config_version") == config_version
        and row.get("status") in {"running", *_TERMINAL_STATUSES}
        and shape is not None
    )
    if row.get("status") == "running":
        valid = (
            valid
            and shape == "reserved"
            and row.get("finished_at") is None
            and row.get("last_completed_step") is None
            and row.get("error") is None
        )
    else:
        valid = valid and row.get("finished_at") is not None
    if not valid:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    return shape


def _registry_valid(registry: object) -> bool:
    return (
        type(registry) is AdapterRegistry
        and callable(registry.greenhouse)
        and callable(registry.lever)
    )


def _eligibility(snapshot: EndpointSnapshot) -> RunnerFailureCode | None:
    if not _snapshot_is_well_formed(snapshot):
        return RunnerFailureCode.ENDPOINT_MALFORMED
    if snapshot.status == "paused":
        return RunnerFailureCode.ENDPOINT_PAUSED
    if snapshot.status == "retired":
        return RunnerFailureCode.ENDPOINT_RETIRED
    if snapshot.kind == "ashby":
        return RunnerFailureCode.ADAPTER_DEFERRED
    if snapshot.kind in _UNIMPLEMENTED:
        return RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED
    if snapshot.kind == "custom":
        return RunnerFailureCode.ADAPTER_UNSUPPORTED
    return None


def _source_endpoint(snapshot: EndpointSnapshot) -> SourceEndpoint:
    return SourceEndpoint(
        kind=snapshot.kind,  # type: ignore[arg-type]
        token=snapshot.token,  # type: ignore[arg-type]
        region=snapshot.region,  # type: ignore[arg-type]
        base_url=snapshot.base_url,  # type: ignore[arg-type]
    )


def _attempt_projection(
    result: IngestionResult,
    *,
    endpoint_id: int,
    adapter_kind: str,
) -> dict[str, object]:
    counts = attempt_counts(result, endpoint_id=endpoint_id)
    counts["adapter_kind"] = adapter_kind
    return counts


def _closed_reason(value: str | None) -> OneEndpointFailureCode | None:
    if value is None:
        return None
    for enum_type in (IngestionReasonCode, TransportFailureCode):
        try:
            return enum_type(value)
        except ValueError:
            continue
    return RunnerFailureCode.INGESTION_RESULT_INVALID


def _closed_failure(value: str | None) -> OneEndpointFailureCode | None:
    if value is None:
        return None
    for enum_type in (RunnerFailureCode, IngestionReasonCode, TransportFailureCode):
        try:
            return enum_type(value)
        except ValueError:
            continue
    raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)


def _matrix(result: IngestionResult) -> tuple[str, OneEndpointFailureCode | None]:
    if (
        not isinstance(result, IngestionResult)
        or type(result.source_fetch_id) is not int  # noqa: E721
        or result.source_fetch_id <= 0
        or type(result.run_id) is not int  # noqa: E721
        or result.run_id <= 0
        or type(result.source_endpoint_id) is not int  # noqa: E721
        or result.source_endpoint_id <= 0
        or (
            result.http_status is not None
            and (
                type(result.http_status) is not int  # noqa: E721
                or not 100 <= result.http_status <= 599
            )
        )
        or any(
            type(value) is not int or value < 0  # noqa: E721
            for value in (
                result.postings_seen,
                result.source_fetch_postings_new,
                result.source_fetch_postings_changed,
            )
        )
    ):
        return "failed", RunnerFailureCode.INGESTION_RESULT_INVALID
    reason = _closed_reason(result.reason_code)
    if reason is RunnerFailureCode.INGESTION_RESULT_INVALID:
        return "failed", reason
    if result.source_fetch_status == "complete":
        if result.http_status != 200 or result.reason_code is not None:
            return "failed", RunnerFailureCode.INGESTION_RESULT_INVALID
        return "succeeded", None
    if result.source_fetch_status == "failed":
        if reason is None or reason in {
            IngestionReasonCode.SUSPICIOUS_ZERO,
            IngestionReasonCode.STALE_OBSERVATION,
        }:
            return "failed", RunnerFailureCode.INGESTION_RESULT_INVALID
        return "failed", reason
    if result.source_fetch_status != "incomplete":
        return "failed", RunnerFailureCode.INGESTION_RESULT_INVALID
    if result.http_status == 304 and reason is None:
        return "succeeded", None
    if reason in {
        IngestionReasonCode.SUSPICIOUS_ZERO,
        IngestionReasonCode.STALE_OBSERVATION,
    }:
        return "partial", reason
    if reason is IngestionReasonCode.INVALID_OBSERVATION:
        return "failed", reason
    if isinstance(reason, TransportFailureCode):
        return "failed", RunnerFailureCode.INGESTION_RESULT_INVALID
    selected = reason or RunnerFailureCode.FETCH_INCOMPLETE
    return ("partial" if result.postings_seen > 0 else "failed"), selected


def _result(
    bound: _Bound,
    *,
    status: str,
    finalized: bool,
    ingestion: IngestionResult | None,
    failure: OneEndpointFailureCode | None,
) -> OneEndpointRunnerResult:
    return OneEndpointRunnerResult(
        run_id=int(bound.run["id"]),
        source_endpoint_id=bound.endpoint_id,
        endpoint_kind=bound.adapter_kind,
        run_status=status,
        finalized=finalized,
        ingestion_result=ingestion,
        failure_code=failure,
    )


def _terminal_step(code: RunnerFailureCode, *, adapter_result: bool = False) -> str:
    if adapter_result or code in {
        RunnerFailureCode.ENDPOINT_DELETED,
        RunnerFailureCode.ENDPOINT_CHANGED,
        RunnerFailureCode.INGESTION_DATABASE_ERROR,
        RunnerFailureCode.INGESTION_INPUT_ERROR,
        RunnerFailureCode.INGESTION_TARGET_ERROR,
    }:
        return "adapter_fetch_completed"
    return "endpoint_snapshotted"


def _locked_bound(bound: _Bound, run: dict[str, Any]) -> _Bound:
    return _Bound(
        run=run,
        endpoint_id=bound.endpoint_id,
        adapter_kind=bound.adapter_kind,
        endpoint=bound.endpoint,
        conditional=bound.conditional,
    )


def _finalize_locked(
    conn: Connection,
    bound: _Bound,
    *,
    status: str,
    step: str,
    counts: dict[str, object],
    failure: OneEndpointFailureCode | None,
    finished_at: datetime,
    ingestion: IngestionResult | None,
) -> OneEndpointRunnerResult:
    desired_error = {"code": failure.value} if failure is not None else None
    finish_run(
        conn,
        run_id=int(bound.run["id"]),
        status=status,
        step=step,
        counts=counts,
        error=desired_error,
        finished_at=finished_at,
    )
    terminal_run = dict(
        bound.run,
        status=status,
        last_completed_step=step,
        counts=counts,
        error=desired_error,
        finished_at=finished_at,
    )
    terminal_bound = _locked_bound(bound, terminal_run)
    logger.info(
        "workflow.one_endpoint.finalized",
        extra={
            "event_name": "workflow.one_endpoint.finalized",
            "run_id": int(terminal_run["id"]),
            "source_endpoint_id": terminal_bound.endpoint_id,
            "adapter_slug": terminal_bound.adapter_kind,
            "run_status": status,
            "failure_code": failure.value if failure else None,
            "attempt_recorded": bool(counts["attempt_recorded"]),
            "postings_seen": counts["postings_seen"],
            "postings_new": counts["postings_new"],
            "postings_changed": counts["postings_changed"],
        },
    )
    return _result(
        terminal_bound,
        status=status,
        finalized=True,
        ingestion=ingestion,
        failure=failure,
    )


def _finish_time(clock: UTCClock, floor: datetime) -> tuple[datetime, bool]:
    try:
        value = _clock_now(clock)
    except ValueError:
        return floor, False
    return (value, True) if value >= floor else (floor, False)


def _finalize_failure_locked(
    conn: Connection,
    bound: _Bound,
    clock: UTCClock,
    code: RunnerFailureCode,
    *,
    adapter_result: bool = False,
    floor: datetime | None = None,
) -> OneEndpointRunnerResult:
    finished, valid = _finish_time(clock, floor or _utc(bound.run["started_at"]))
    selected = code if valid else RunnerFailureCode.CLOCK_INVALID
    return _finalize_locked(
        conn,
        bound,
        status="failed",
        step=_terminal_step(selected, adapter_result=adapter_result),
        counts=reserved_counts(bound.endpoint_id, bound.adapter_kind),
        failure=selected,
        finished_at=finished,
        ingestion=None,
    )


def _validate_terminal_without_fetch(bound: _Bound) -> OneEndpointRunnerResult:
    row = bound.run
    code = _error_code(row.get("error"))
    allowed = {
        item.value
        for item in RunnerFailureCode
        if item
        not in {
            RunnerFailureCode.INVALID_ENDPOINT_ID,
            RunnerFailureCode.INVALID_RUN_ID,
            RunnerFailureCode.INVALID_CONFIG_VERSION,
            RunnerFailureCode.INVALID_CLOCK,
            RunnerFailureCode.PIPELINE_RUN_NOT_FOUND,
            RunnerFailureCode.SOURCE_ENDPOINT_NOT_FOUND,
            RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE,
            RunnerFailureCode.DATABASE_ERROR,
            RunnerFailureCode.FETCH_INCOMPLETE,
            RunnerFailureCode.FINALIZATION_DATABASE_ERROR,
            RunnerFailureCode.FINALIZATION_CONFLICT,
        }
    }
    endpoint_steps = {
        RunnerFailureCode.ENDPOINT_PAUSED,
        RunnerFailureCode.ENDPOINT_RETIRED,
        RunnerFailureCode.ADAPTER_DEFERRED,
        RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED,
        RunnerFailureCode.ADAPTER_UNSUPPORTED,
        RunnerFailureCode.ENDPOINT_MALFORMED,
        RunnerFailureCode.ENDPOINT_POLICY,
        RunnerFailureCode.ADAPTER_REGISTRY_DEFECT,
        RunnerFailureCode.ADAPTER_DEFECT,
        RunnerFailureCode.CANCELLED,
    }
    adapter_steps = {
        RunnerFailureCode.ENDPOINT_DELETED,
        RunnerFailureCode.ENDPOINT_CHANGED,
        RunnerFailureCode.INGESTION_DATABASE_ERROR,
        RunnerFailureCode.INGESTION_INPUT_ERROR,
        RunnerFailureCode.INGESTION_TARGET_ERROR,
        RunnerFailureCode.INGESTION_RESULT_INVALID,
    }
    parsed_code = RunnerFailureCode(code) if code in allowed else None
    expected_steps = (
        {"endpoint_snapshotted"}
        if parsed_code in endpoint_steps
        else {"adapter_fetch_completed"}
        if parsed_code in adapter_steps
        else {"endpoint_snapshotted", "adapter_fetch_completed"}
        if parsed_code is RunnerFailureCode.CLOCK_INVALID
        else set()
    )
    if (
        row.get("status") != "failed"
        or parsed_code is None
        or row.get("last_completed_step") not in expected_steps
        or _counts_shape(
            row.get("counts"),
            endpoint_id=bound.endpoint_id,
            adapter_kind=bound.adapter_kind,
        )
        != "reserved"
    ):
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    return _result(
        bound,
        status="failed",
        finalized=True,
        ingestion=None,
        failure=parsed_code,
    )


def _validate_attempt_relationship(bound: _Bound, replay: IngestionResult) -> None:
    if replay.run_id != int(bound.run["id"]) or replay.source_endpoint_id != bound.endpoint_id:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    if bound.run["status"] in _TERMINAL_STATUSES:
        expected = _attempt_projection(
            replay,
            endpoint_id=bound.endpoint_id,
            adapter_kind=bound.adapter_kind,
        )
        if bound.run["counts"] != expected:
            raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
        status, failure = _matrix(replay)
        persisted_failure = _closed_failure(_error_code(bound.run["error"]))
        if persisted_failure in {
            RunnerFailureCode.CLOCK_INVALID,
            RunnerFailureCode.INGESTION_RESULT_INVALID,
        }:
            status = "failed"
            failure = persisted_failure
        if (
            bound.run["status"] != status
            or bound.run["last_completed_step"] != "source_fetch_recorded"
            or _error_code(bound.run["error"]) != (failure.value if failure is not None else None)
        ):
            raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)


def _persisted_adapter_kind(
    run: dict[str, Any],
    *,
    endpoint_id: int,
    config_version: str,
) -> str:
    counts = run.get("counts")
    adapter_kind = counts.get("adapter_kind") if type(counts) is dict else None  # noqa: E721
    if type(adapter_kind) is not str or adapter_kind not in {*_KINDS, "malformed"}:  # noqa: E721
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    _validate_owned_run(
        run,
        endpoint_id=endpoint_id,
        adapter_kind=adapter_kind,
        config_version=config_version,
    )
    return adapter_kind


def _bind(
    *,
    engine: Engine,
    endpoint_id: int,
    run_id: int | None,
    config_version: str,
    started_at: datetime | None,
) -> _Bound:
    try:
        with engine.begin() as conn:
            if run_id is not None:
                run = lock_run(conn, run_id)
                if run is None:
                    raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_NOT_FOUND)
                adapter_kind = _persisted_adapter_kind(
                    run,
                    endpoint_id=endpoint_id,
                    config_version=config_version,
                )
                fetch = matching_fetch_row(conn, run_id=run_id, endpoint_id=endpoint_id)
                if run["status"] in _TERMINAL_STATUSES or fetch is not None:
                    return _Bound(
                        run=run,
                        endpoint_id=endpoint_id,
                        adapter_kind=adapter_kind,
                        endpoint=None,
                        conditional=None,
                    )
                endpoint = lock_endpoint(conn, endpoint_id)
                if endpoint is None:
                    raise RunnerTargetError(RunnerFailureCode.SOURCE_ENDPOINT_NOT_FOUND)
                if _kind(endpoint) != adapter_kind:
                    raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
            else:
                endpoint = lock_endpoint(conn, endpoint_id)
                if endpoint is None:
                    raise RunnerTargetError(RunnerFailureCode.SOURCE_ENDPOINT_NOT_FOUND)
                adapter_kind = _kind(endpoint)
                assert started_at is not None
                run = create_run(
                    conn,
                    endpoint_id=endpoint_id,
                    adapter_kind=adapter_kind,
                    config_version=config_version,
                    started_at=started_at,
                )
            rows = validator_rows(conn, endpoint_id=endpoint_id, excluded_run_id=int(run["id"]))
            conditional = reconstruct_validators(rows)
            return _Bound(
                run=run,
                endpoint_id=endpoint_id,
                adapter_kind=adapter_kind,
                endpoint=endpoint,
                conditional=conditional,
            )
    except RunnerTargetError:
        raise
    except SQLAlchemyError:
        raise RunnerDatabaseError(RunnerFailureCode.DATABASE_ERROR, run_id=run_id) from None


def _replay(engine: Engine, bound: _Bound) -> IngestionResult | None:
    try:
        return IngestionService(engine).lookup_replay_result(
            run_id=int(bound.run["id"]),
            source_endpoint_id=bound.endpoint_id,
        )
    except (IngestionInputError, IngestionDatabaseError, SQLAlchemyError):
        raise RunnerDatabaseError(
            RunnerFailureCode.DATABASE_ERROR, run_id=int(bound.run["id"])
        ) from None


def _validate_fetch_row(
    bound: _Bound,
    replay: IngestionResult,
    row: dict[str, Any] | None,
) -> datetime:
    if row is None:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    try:
        started = _utc(row.get("started_at"))
        finished = _utc(row.get("finished_at"))
        run_started = _utc(bound.run.get("started_at"))
        if not run_started <= started <= finished:
            raise ValueError
        if bound.run.get("finished_at") is not None and finished > _utc(bound.run["finished_at"]):
            raise ValueError
    except ValueError:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE) from None
    expected_error = {"code": replay.reason_code} if replay.reason_code is not None else None
    if (
        int(row["id"]) != replay.source_fetch_id
        or row["status"] != replay.source_fetch_status
        or row["http_status"] != replay.http_status
        or row["postings_seen"] != replay.postings_seen
        or row["postings_new"] != replay.source_fetch_postings_new
        or row["postings_changed"] != replay.source_fetch_postings_changed
        or row["error"] != expected_error
    ):
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    return finished


def _recheck(engine: Engine, bound: _Bound) -> RunnerFailureCode | None:
    assert bound.endpoint is not None
    try:
        with engine.begin() as conn:
            current = lock_endpoint(conn, bound.endpoint_id)
            if current is None:
                return RunnerFailureCode.ENDPOINT_DELETED
            if current != bound.endpoint:
                return RunnerFailureCode.ENDPOINT_CHANGED
    except SQLAlchemyError:
        raise RunnerDatabaseError(
            RunnerFailureCode.DATABASE_ERROR, run_id=int(bound.run["id"])
        ) from None
    return None


def _fresh_result_matches(
    fresh: object,
    persisted: IngestionResult,
    bound: _Bound,
) -> bool:
    return (
        isinstance(fresh, IngestionResult)
        and fresh.run_id == int(bound.run["id"])
        and fresh.source_endpoint_id == bound.endpoint_id
        and fresh.source_fetch_id == persisted.source_fetch_id
        and fresh.source_fetch_status == persisted.source_fetch_status
        and fresh.http_status == persisted.http_status
        and fresh.postings_seen == persisted.postings_seen
        and fresh.source_fetch_postings_new == persisted.source_fetch_postings_new
        and fresh.source_fetch_postings_changed == persisted.source_fetch_postings_changed
        and fresh.reason_code == persisted.reason_code
    )


def _persisted_attempt(
    engine: Engine,
    bound: _Bound,
    row: dict[str, Any],
) -> tuple[IngestionResult, datetime]:
    replay = _replay(engine, bound)
    if replay is None:
        raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
    finished_at = _validate_fetch_row(bound, replay, row)
    return replay, finished_at


def _coordinate(
    engine: Engine,
    bound: _Bound,
    clock: UTCClock,
    *,
    local_failure: RunnerFailureCode | None = None,
    adapter_result: bool = False,
    finish_floor: datetime | None = None,
    fetch_result: FetchResult | None = None,
    transport: TransportFailureCode | None = None,
    attempt_started: datetime | None = None,
    attempt_finished: datetime | None = None,
) -> OneEndpointRunnerResult:
    """Serialize the durable winner while holding only the owned run lock."""

    result_ingestion: IngestionResult | None = None
    try:
        with engine.begin() as conn:
            current = lock_run(conn, int(bound.run["id"]))
            if current is None:
                raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)
            _validate_owned_run(
                current,
                endpoint_id=bound.endpoint_id,
                adapter_kind=bound.adapter_kind,
                config_version=str(bound.run["config_version"]),
            )
            locked = _locked_bound(bound, current)
            row = matching_fetch_row(
                conn,
                run_id=int(current["id"]),
                endpoint_id=bound.endpoint_id,
            )

            if current["status"] in _TERMINAL_STATUSES:
                if row is None:
                    return _validate_terminal_without_fetch(locked)
                replay, _ = _persisted_attempt(engine, locked, row)
                _validate_attempt_relationship(locked, replay)
                return _result(
                    locked,
                    status=str(current["status"]),
                    finalized=True,
                    ingestion=replay,
                    failure=_closed_failure(_error_code(current["error"])),
                )

            if row is not None:
                replay, persisted_finish = _persisted_attempt(engine, locked, row)
                result_ingestion = replay
                status, failure = _matrix(replay)
                finish, valid = _finish_time(clock, persisted_finish)
                if not valid:
                    status = "failed"
                    failure = RunnerFailureCode.CLOCK_INVALID
                    finish = persisted_finish
                return _finalize_locked(
                    conn,
                    locked,
                    status=status,
                    step="source_fetch_recorded",
                    counts=_attempt_projection(
                        replay,
                        endpoint_id=locked.endpoint_id,
                        adapter_kind=locked.adapter_kind,
                    ),
                    failure=failure,
                    finished_at=finish,
                    ingestion=replay,
                )

            if local_failure is not None:
                return _finalize_failure_locked(
                    conn,
                    locked,
                    clock,
                    local_failure,
                    adapter_result=adapter_result,
                    floor=finish_floor,
                )

            if attempt_started is None or attempt_finished is None:
                raise RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)

            ingestion: object | None = None
            ingestion_failure: RunnerFailureCode | None = None
            service = IngestionService(engine)
            try:
                if transport is not None:
                    ingestion = service.record_fetch_failure(
                        run_id=int(current["id"]),
                        source_endpoint_id=locked.endpoint_id,
                        started_at=attempt_started,
                        finished_at=attempt_finished,
                        reason_code=transport,
                    )
                else:
                    ingestion = service.ingest_fetch_result(
                        run_id=int(current["id"]),
                        source_endpoint_id=locked.endpoint_id,
                        fetch_result=fetch_result,  # type: ignore[arg-type]
                        started_at=attempt_started,
                        finished_at=attempt_finished,
                    )
            except IngestionDatabaseError:
                ingestion_failure = RunnerFailureCode.INGESTION_DATABASE_ERROR
            except IngestionInputError:
                ingestion_failure = RunnerFailureCode.INGESTION_INPUT_ERROR
            except IngestionTargetError:
                ingestion_failure = RunnerFailureCode.INGESTION_TARGET_ERROR

            row = matching_fetch_row(
                conn,
                run_id=int(current["id"]),
                endpoint_id=locked.endpoint_id,
            )
            if row is None:
                return _finalize_failure_locked(
                    conn,
                    locked,
                    clock,
                    ingestion_failure or RunnerFailureCode.INGESTION_RESULT_INVALID,
                    adapter_result=True,
                    floor=attempt_finished,
                )

            replay, persisted_finish = _persisted_attempt(engine, locked, row)
            status, failure = _matrix(replay)
            fresh_valid = ingestion_failure is not None or _fresh_result_matches(
                ingestion,
                replay,
                locked,
            )
            selected_ingestion = ingestion if ingestion_failure is None and fresh_valid else replay
            result_ingestion = selected_ingestion  # type: ignore[assignment]
            if not fresh_valid:
                status = "failed"
                failure = RunnerFailureCode.INGESTION_RESULT_INVALID
            finish, valid = _finish_time(clock, persisted_finish)
            if not valid:
                status = "failed"
                failure = RunnerFailureCode.CLOCK_INVALID
                finish = persisted_finish
            return _finalize_locked(
                conn,
                locked,
                status=status,
                step="source_fetch_recorded",
                counts=_attempt_projection(
                    replay,
                    endpoint_id=locked.endpoint_id,
                    adapter_kind=locked.adapter_kind,
                ),
                failure=failure,
                finished_at=finish,
                ingestion=selected_ingestion,  # type: ignore[arg-type]
            )
    except SQLAlchemyError:
        return _result(
            bound,
            status="running",
            finalized=False,
            ingestion=result_ingestion,
            failure=RunnerFailureCode.FINALIZATION_DATABASE_ERROR,
        )


async def run_one_endpoint(
    *,
    engine: Engine,
    source_endpoint_id: int,
    run_id: int | None,
    config_version: str,
    adapters: AdapterRegistry,
    clock: UTCClock,
) -> OneEndpointRunnerResult:
    """Run one exact endpoint and finalize one runner-owned manual run."""

    endpoint_id, requested_run_id, config = _validate_inputs(
        source_endpoint_id, run_id, config_version
    )
    started_at = None
    if requested_run_id is None:
        try:
            started_at = _clock_now(clock)
        except ValueError:
            raise RunnerInputError(RunnerFailureCode.INVALID_CLOCK) from None
    bound = _bind(
        engine=engine,
        endpoint_id=endpoint_id,
        run_id=requested_run_id,
        config_version=config,
        started_at=started_at,
    )

    if bound.endpoint is None:
        return _coordinate(engine, bound, clock)

    exclusion = _eligibility(bound.endpoint)
    if exclusion is not None:
        return _coordinate(engine, bound, clock, local_failure=exclusion)
    if not _registry_valid(adapters):
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.ADAPTER_REGISTRY_DEFECT,
        )

    factory = adapters.greenhouse if bound.endpoint.kind == "greenhouse" else adapters.lever
    try:
        adapter = factory()
        if adapter.slug != bound.endpoint.kind or not callable(adapter.list_postings):
            raise TypeError
    except Exception:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.ADAPTER_REGISTRY_DEFECT,
        )

    run_started = _utc(bound.run["started_at"])
    try:
        attempt_started = _clock_now(clock)
        if attempt_started < run_started:
            raise ValueError
    except ValueError:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.CLOCK_INVALID,
        )

    fetch_result: FetchResult | None = None
    transport: TransportFailureCode | None = None
    unknown_transport = False
    try:
        fetch_result = await adapter.list_postings(
            _source_endpoint(bound.endpoint),
            bound.conditional,  # type: ignore[arg-type]
        )
    except asyncio.CancelledError:
        try:
            _coordinate(
                engine,
                bound,
                clock,
                local_failure=RunnerFailureCode.CANCELLED,
            )
        except Exception:
            pass
        raise
    except (GreenhouseEndpointError, LeverEndpointError):
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.ENDPOINT_POLICY,
        )
    except (GreenhouseTransportError, LeverTransportError) as exc:
        transport = _TRANSPORT_CATEGORIES.get(exc.category)
        unknown_transport = transport is None
    except Exception:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.ADAPTER_DEFECT,
        )

    try:
        attempt_finished = _clock_now(clock)
        if attempt_finished < attempt_started:
            raise ValueError
    except ValueError:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.CLOCK_INVALID,
            adapter_result=True,
            finish_floor=attempt_started,
        )

    changed = _recheck(engine, bound)
    if changed is not None:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=changed,
            adapter_result=True,
            finish_floor=attempt_finished,
        )
    if unknown_transport:
        return _coordinate(
            engine,
            bound,
            clock,
            local_failure=RunnerFailureCode.ADAPTER_DEFECT,
            adapter_result=True,
            finish_floor=attempt_finished,
        )
    return _coordinate(
        engine,
        bound,
        clock,
        fetch_result=fetch_result,
        transport=transport,
        attempt_started=attempt_started,
        attempt_finished=attempt_finished,
    )
