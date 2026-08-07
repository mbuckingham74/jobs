"""Closed public result and error vocabulary for hard-filter evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.filters.evaluator import (
        HardFilterReason,
        HardFilterRuleOutcome,
        HardFilterUnknown,
        StalenessSource,
    )


class FilterErrorCode(str, Enum):
    INVALID_INPUT = "filter.invalid_input"
    POSTING_NOT_FOUND = "filter.posting_not_found"
    POSTING_CLOSED = "filter.posting_closed"
    CURRENT_VERSION_INVALID = "filter.current_version_invalid"
    PERSISTED_INPUT_INVALID = "filter.persisted_input_invalid"
    POLICY_UNSUPPORTED = "filter.policy_unsupported"
    INPUT_STALE = "filter.input_stale"
    EVALUATION_CONFLICT = "filter.evaluation_conflict"
    DATABASE_ERROR = "filter.database_error"
    EVALUATOR_DEFECT = "filter.evaluator_defect"


class FilterError(Exception):
    def __init__(self, code: FilterErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class FilterInputError(FilterError):
    pass


class PostingNotFoundError(FilterError):
    pass


class PostingClosedError(FilterError):
    pass


class CurrentVersionError(FilterError):
    pass


class FilterPersistedInputError(FilterError):
    pass


class UnsupportedFilterPolicyError(FilterError):
    pass


class StaleFilterInputError(FilterError):
    pass


class FilterEvaluationConflictError(FilterError):
    pass


class FilterDatabaseError(FilterError):
    pass


class FilterEvaluatorDefectError(FilterError):
    pass


@dataclass(frozen=True)
class HardFilterResult:
    evaluation_id: int
    posting_id: int
    posting_version_id: int
    policy_version: str
    policy_manifest_hash: str
    mutable_state_hash: str
    input_hash: str
    result_hash: str
    evaluation_as_of: datetime
    eligible: bool
    rejection_reasons: tuple[HardFilterReason, ...]
    unknowns: tuple[HardFilterUnknown, ...]
    staleness_source: StalenessSource
    rule_outcomes: tuple[HardFilterRuleOutcome, ...]
    replayed: bool


__all__ = [
    "FilterDatabaseError",
    "FilterError",
    "FilterErrorCode",
    "FilterEvaluationConflictError",
    "FilterEvaluatorDefectError",
    "FilterInputError",
    "FilterPersistedInputError",
    "PostingClosedError",
    "PostingNotFoundError",
    "CurrentVersionError",
    "StaleFilterInputError",
    "UnsupportedFilterPolicyError",
    "HardFilterResult",
]
