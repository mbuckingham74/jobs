"""Sensitive ingestion values never enter structured logs or results."""

from __future__ import annotations

import json
import logging

from app.ingestion.results import IngestionResult
from app.ingestion.service import IngestionService


def test_result_logging_contains_only_safe_fields(caplog) -> None:
    result = IngestionResult(
        source_fetch_id=1,
        run_id=2,
        source_endpoint_id=3,
        source_fetch_status="complete",
        http_status=200,
        postings_seen=1,
        source_fetch_postings_new=1,
        source_fetch_postings_changed=0,
        reason_code=None,
        postings_created=1,
        postings_updated=0,
        versions_created=1,
        postings_closed=0,
        postings_reopened=0,
        created_posting_version_ids=(4,),
        reconciliation_ran=True,
        replayed=False,
        endpoint_marked_failing=False,
        endpoint_marked_active=False,
        alert_required=False,
        alert_code=None,
    )
    sentinels = [
        "secret-endpoint-token",
        "external-job-id",
        "Sensitive Job Title",
        "private description",
        "https://private.invalid/apply",
        "validator-secret",
        "postgresql+psycopg://user:password@db/jobs",
    ]
    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        IngestionService._log_result(result)
    serialized = json.dumps([record.__dict__ for record in caplog.records], default=str)
    for sentinel in sentinels:
        assert sentinel not in serialized
        assert sentinel not in repr(result)
