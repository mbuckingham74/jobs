"""Closed, privacy-safe result and error types for ATS ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TransportFailureCode(str, Enum):
    CONNECT_TIMEOUT = "adapter.connect_timeout"
    READ_TIMEOUT = "adapter.read_timeout"
    WRITE_TIMEOUT = "adapter.write_timeout"
    POOL_TIMEOUT = "adapter.pool_timeout"
    CONNECT_ERROR = "adapter.connect_error"
    READ_ERROR = "adapter.read_error"
    WRITE_ERROR = "adapter.write_error"
    PROTOCOL_ERROR = "adapter.protocol_error"
    TLS_ERROR = "adapter.tls_error"
    TRANSPORT_ERROR = "adapter.transport_error"


class IngestionError(Exception):
    """Bounded public error carrying one closed reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IngestionInputError(IngestionError):
    """The callable metadata or failure code is invalid."""


class IngestionTargetError(IngestionError):
    """A required run or endpoint does not exist."""


class IngestionDatabaseError(IngestionError):
    """A database operation failed without exposing SQL or parameters."""


@dataclass(frozen=True)
class IngestionResult:
    source_fetch_id: int
    run_id: int
    source_endpoint_id: int
    source_fetch_status: str
    http_status: int | None
    postings_seen: int
    source_fetch_postings_new: int
    source_fetch_postings_changed: int
    reason_code: str | None
    postings_created: int
    postings_updated: int
    versions_created: int
    postings_closed: int
    postings_reopened: int
    created_posting_version_ids: tuple[int, ...]
    reconciliation_ran: bool
    replayed: bool
    endpoint_marked_failing: bool
    endpoint_marked_active: bool
    alert_required: bool
    alert_code: str | None
