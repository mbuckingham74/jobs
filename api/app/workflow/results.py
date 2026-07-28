"""Closed, privacy-safe result and error types for Task 007."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.ingestion.results import IngestionResult, TransportFailureCode


class RunnerFailureCode(str, Enum):
    INVALID_ENDPOINT_ID = "runner.invalid_endpoint_id"
    INVALID_RUN_ID = "runner.invalid_run_id"
    INVALID_CONFIG_VERSION = "runner.invalid_config_version"
    INVALID_CLOCK = "runner.invalid_clock"
    PIPELINE_RUN_NOT_FOUND = "runner.pipeline_run_not_found"
    SOURCE_ENDPOINT_NOT_FOUND = "runner.source_endpoint_not_found"
    PIPELINE_RUN_INCOMPATIBLE = "runner.pipeline_run_incompatible"
    DATABASE_ERROR = "runner.database_error"
    ENDPOINT_PAUSED = "runner.endpoint_paused"
    ENDPOINT_RETIRED = "runner.endpoint_retired"
    ADAPTER_DEFERRED = "runner.adapter_deferred"
    ADAPTER_NOT_IMPLEMENTED = "runner.adapter_not_implemented"
    ADAPTER_UNSUPPORTED = "runner.adapter_unsupported"
    ENDPOINT_MALFORMED = "runner.endpoint_malformed"
    ENDPOINT_POLICY = "runner.endpoint_policy"
    ENDPOINT_DELETED = "runner.endpoint_deleted"
    ENDPOINT_CHANGED = "runner.endpoint_changed"
    ADAPTER_REGISTRY_DEFECT = "runner.adapter_registry_defect"
    ADAPTER_DEFECT = "runner.adapter_defect"
    CLOCK_INVALID = "runner.clock_invalid"
    INGESTION_DATABASE_ERROR = "runner.ingestion_database_error"
    INGESTION_INPUT_ERROR = "runner.ingestion_input_error"
    INGESTION_TARGET_ERROR = "runner.ingestion_target_error"
    INGESTION_RESULT_INVALID = "runner.ingestion_result_invalid"
    FETCH_INCOMPLETE = "runner.fetch_incomplete"
    CANCELLED = "runner.cancelled"
    FINALIZATION_DATABASE_ERROR = "runner.finalization_database_error"
    FINALIZATION_CONFLICT = "runner.finalization_conflict"


class IngestionReasonCode(str, Enum):
    INVALID_OBSERVATION = "ingestion.invalid_observation"
    SUSPICIOUS_ZERO = "ingestion.suspicious_zero"
    STALE_OBSERVATION = "ingestion.stale_observation"


type OneEndpointFailureCode = RunnerFailureCode | TransportFailureCode | IngestionReasonCode


class RunnerError(Exception):
    """A bounded public error carrying no raw database or provider text."""

    def __init__(self, code: RunnerFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


class RunnerInputError(RunnerError):
    """Invalid invocation metadata before a run is bound."""


class RunnerTargetError(RunnerError):
    """A requested run or endpoint cannot be used."""


class RunnerDatabaseError(RunnerError):
    """A database phase failed before a result could be returned."""

    def __init__(self, code: RunnerFailureCode, run_id: int | None = None) -> None:
        super().__init__(code)
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class OneEndpointRunnerResult:
    run_id: int
    source_endpoint_id: int
    endpoint_kind: str
    run_status: str
    finalized: bool
    ingestion_result: IngestionResult | None
    failure_code: OneEndpointFailureCode | None
