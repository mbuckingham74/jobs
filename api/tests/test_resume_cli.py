"""Tests for the ``jobs-resume-sync`` console script.

Exercises CLI serialization with test doubles or local synthetic fixtures only;
never uses the production URL as a validation dependency. Covers every
exit-code/status mapping and asserts the stdout JSON payload carries only safe
fields (no ``content_md``, PDF bytes, headers, validators, database URLs, or
credentials).
"""

from __future__ import annotations

import io
import json

import pytest

from app.resume.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    run_once,
)
from app.resume.results import ResumeSyncStatus


def _config_error_environ() -> dict[str, str]:
    return {"DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/jobs"}


# ---------------------------------------------------------------------
# Non-PostgreSQL CLI tests: configuration, fetch, and extraction outcomes
# never require a database.
# ---------------------------------------------------------------------


def test_cli_config_error_exit_code_and_json() -> None:
    out = io.StringIO()
    code = run_once(environ=_config_error_environ(), stdout=out)
    payload = json.loads(out.getvalue())
    assert code == EXIT_CONFIG_ERROR
    assert payload["status"] == ResumeSyncStatus.CONFIG_ERROR.value
    assert payload["error"]["code"] == "config.invalid"
    assert "content_md" not in payload


def test_cli_fetch_rejected_exit_nonzero() -> None:
    from app.resume.fetch import ConditionalValidators, FetchOutcome, FetchOutcomeKind

    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/jobs",
            "BASE_RESUME_URL": "https://example.invalid/x.pdf",
        },
        stdout=out,
        fetch_outcome_override=FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=500,
            reason="fetcher.non_200_status",
        ),
        pre_fetch_validators_override=ConditionalValidators(),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_FAILURE
    assert payload["status"] == ResumeSyncStatus.FETCH_REJECTED.value
    assert payload["http_status"] == 500


def test_cli_fetch_error_exit_nonzero() -> None:
    from app.resume.fetch import ConditionalValidators, FetchOutcome, FetchOutcomeKind

    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/jobs",
            "BASE_RESUME_URL": "https://example.invalid/x.pdf",
        },
        stdout=out,
        fetch_outcome_override=FetchOutcome(
            kind=FetchOutcomeKind.ERROR,
            reason="fetcher.connect_timeout",
        ),
        pre_fetch_validators_override=ConditionalValidators(),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_FAILURE
    assert payload["status"] == ResumeSyncStatus.FETCH_ERROR.value


def test_cli_extraction_rejected_exit_nonzero() -> None:
    from app.resume.extract import ExtractionRejection
    from app.resume.fetch import ConditionalValidators, FetchOutcome, FetchOutcomeKind

    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/jobs",
            "BASE_RESUME_URL": "https://example.invalid/x.pdf",
        },
        stdout=out,
        fetch_outcome_override=FetchOutcome(
            kind=FetchOutcomeKind.OK,
            http_status=200,
            byte_count=100,
            body=b"%PDF-1.4 " + b"x" * 91,  # 9 + 91 == 100 bytes
            fetched_at=None,
        ),
        pre_fetch_validators_override=ConditionalValidators(),
        extractor=lambda body: ExtractionRejection(
            code="extraction.document_too_few_letters",
            page_count=1,
            byte_count=len(body),
            content_char_count=10,
            content_token_count=2,
        ),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_FAILURE
    assert payload["status"] == ResumeSyncStatus.EXTRACTION_REJECTED.value
    assert payload["page_count"] == 1
    assert payload["byte_count"] == 100
    # No resume_version_id on a rejection.
    assert "resume_version_id" not in payload


# ---------------------------------------------------------------------
# PostgreSQL CLI tests: success and DB-failure statuses that require a
# disposable PostgreSQL database. Skipped when TEST_DATABASE_URL is unset.
# ---------------------------------------------------------------------

_pg = pytest.mark.postgres


@_pg
def test_cli_activated_initial_exit_zero(postgres_engine) -> None:
    from tests.resume_test_helpers import CONTENT_A, make_extraction_result, ok_outcome

    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
            "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
        },
        stdout=out,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_OK
    assert payload["status"] == ResumeSyncStatus.ACTIVATED_INITIAL.value
    assert payload["activated"] is True
    assert payload["http_status"] == 200


@_pg
def test_cli_staged_for_review_exit_zero(postgres_engine) -> None:
    from tests.resume_test_helpers import (
        CONTENT_A,
        CONTENT_B,
        make_extraction_result,
        ok_outcome,
    )

    # First import to activate the base.
    run_once(
        environ={
            "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
            "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
        },
        stdout=io.StringIO(),
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
            "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
        },
        stdout=out,
        fetch_outcome_override=ok_outcome(b"body-b"),
        extractor=lambda body: make_extraction_result(CONTENT_B),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_OK
    assert payload["status"] == ResumeSyncStatus.STAGED_FOR_REVIEW.value
    assert payload["activated"] is False


@_pg
def test_cli_unchanged_content_exit_zero(postgres_engine) -> None:
    from tests.resume_test_helpers import CONTENT_A, make_extraction_result, ok_outcome

    environ = {
        "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
        "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
    }
    run_once(
        environ=environ,
        stdout=io.StringIO(),
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    out = io.StringIO()
    code = run_once(
        environ=environ,
        stdout=out,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_OK
    assert payload["status"] == ResumeSyncStatus.UNCHANGED_CONTENT.value
    assert payload["content_changed"] is False


@_pg
def test_cli_not_modified_exit_zero(postgres_engine) -> None:
    from tests.resume_test_helpers import (
        CONTENT_A,
        make_extraction_result,
        not_modified_outcome,
        ok_outcome,
    )

    environ = {
        "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
        "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
    }
    run_once(
        environ=environ,
        stdout=io.StringIO(),
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: make_extraction_result(CONTENT_A),
    )
    out = io.StringIO()
    code = run_once(environ=environ, stdout=out, fetch_outcome_override=not_modified_outcome())
    payload = json.loads(out.getvalue())
    assert code == EXIT_OK
    assert payload["status"] == ResumeSyncStatus.NOT_MODIFIED.value
    assert payload["http_status"] == 304


@_pg
def test_cli_provenance_conflict_exit_nonzero(postgres_engine) -> None:
    from sqlalchemy import insert

    from app.resume import repository as repo
    from tests.resume_test_helpers import (
        CONTENT_A,
        make_extraction_result,
        ok_outcome,
    )

    extraction = make_extraction_result(CONTENT_A)
    with postgres_engine.begin() as conn:
        conn.execute(
            insert(repo.resume_version_table).values(
                variant="technical",
                source_kind="manual",
                content_hash=extraction.content_hash,
                content_md=extraction.content_md,
                active=False,
            )
        )
    out = io.StringIO()
    code = run_once(
        environ={
            "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
            "BASE_RESUME_URL": "https://example.invalid/portfolio.pdf",
        },
        stdout=out,
        fetch_outcome_override=ok_outcome(b"body-a"),
        extractor=lambda body: extraction,
    )
    payload = json.loads(out.getvalue())
    assert code == EXIT_FAILURE
    assert payload["status"] == ResumeSyncStatus.PROVENANCE_CONFLICT.value


# ---------------------------------------------------------------------
# Payload privacy: every CLI result object omits unsafe fields.
# ---------------------------------------------------------------------


def test_payload_never_carries_content_md_or_bytes() -> None:
    from app.resume.cli import result_to_payload
    from app.resume.results import ResumeSyncResult, ResumeSyncStatus

    payload = result_to_payload(
        ResumeSyncResult(
            status=ResumeSyncStatus.FETCH_REJECTED,
            http_status=500,
            error=None,
        )
    )
    assert "content_md" not in payload
    assert "body" not in payload
    assert "headers" not in payload
    assert "database_url" not in payload
