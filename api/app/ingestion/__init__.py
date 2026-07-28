"""Idempotent ATS ingestion without adapter or network coupling."""

from app.ingestion.results import (
    IngestionDatabaseError,
    IngestionInputError,
    IngestionResult,
    IngestionTargetError,
    TransportFailureCode,
)
from app.ingestion.service import (
    IngestionService,
    ingest_fetch_result,
    record_fetch_failure,
)

__all__ = [
    "IngestionDatabaseError",
    "IngestionInputError",
    "IngestionResult",
    "IngestionService",
    "IngestionTargetError",
    "TransportFailureCode",
    "ingest_fetch_result",
    "record_fetch_failure",
]
