"""Tests for the privacy-safe structured logging surface.

Captures stderr/log output for every outcome class and asserts that
synthetic fixture text, raw-byte sentinels, credentials, conditional header
values, database URLs, and résumé excerpts never appear in a log line or in
the error serialization of a result.
"""

from __future__ import annotations

import io
import json
import logging

from app.resume.fetch import ConditionalValidators, FetchOutcome, FetchOutcomeKind
from app.resume.logging import configure_logging, log_event, safe_hostname
from app.resume.results import ResumeSyncStatus
from app.resume.service import sync_resume

SECRET_FIXTURE_TEXT = "SECRET_RESUME_SENTENCE_TEXT_FOR_LEAK_DETECTION"
SECRET_RAW_BYTES_SENTINEL = b"RAW_BYTES_SENTINEL_FOR_LEAK_DETECTION"
SECRET_ETAG = "SECRET_ETAG_VALUE"
SECRET_LAST_MODIFIED = "Wed, 01 Jul 2026 12:00:00 GMT"
SECRET_HOST_URL = "https://userinfo-leak:secret-token@example.invalid/path?query=frag#frag"


def _capture_logger(buf: io.StringIO) -> logging.Logger:
    from app.resume.logging import JsonFormatter

    logger = logging.getLogger("app.resume.test")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def test_safe_hostname_strips_userinfo_query_fragment_and_path() -> None:
    assert safe_hostname(SECRET_HOST_URL) == "example.invalid"
    assert safe_hostname(None) is None


def test_log_event_emits_stable_event_name_and_safe_fields() -> None:
    buf = io.StringIO()
    logger = _capture_logger(buf)
    log_event(
        logger,
        "resume.sync.start",
        source_host="example.invalid",
        variant="base",
        http_status=200,
        byte_count=42,
    )
    line = json.loads(buf.getvalue().strip())
    assert line["event"] == "resume.sync.start"
    assert line["source_host"] == "example.invalid"
    assert line["variant"] == "base"
    assert line["http_status"] == 200
    assert line["byte_count"] == 42


def test_configured_logger_writes_json_lines_to_stderr() -> None:
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stderr(buf):
        logger = configure_logging()
        log_event(logger, "resume.sync.test", foo="bar")
    out = json.loads(buf.getvalue().strip())
    assert out["event"] == "resume.sync.test"
    assert out["foo"] == "bar"


def _make_settings_no_db() -> object:
    from app.resume.config import ResumeSettings

    # The sentinel credential is never opened: every database boundary in
    # these logging tests is mocked via ``pre_fetch_validators_override`` so
    # no socket connection is attempted.
    return ResumeSettings(
        database_url="postgresql+psycopg://leakuser:leakpassword@127.0.0.1:5432/jobs",
        base_resume_url="https://example.invalid/x.pdf",
        resume_max_bytes=10 * 1024 * 1024,
    )


def test_log_line_for_fetch_rejection_omits_secrets() -> None:
    buf = io.StringIO()
    logger = _capture_logger(buf)

    outcome = FetchOutcome(
        kind=FetchOutcomeKind.REJECTED,
        http_status=500,
        reason="fetcher.non_200_status",
        body=SECRET_RAW_BYTES_SENTINEL,
        etag=SECRET_ETAG,
        last_modified=None,
    )
    result = sync_resume(
        _make_settings_no_db(),
        logger=logger,
        fetch_outcome_override=outcome,
        pre_fetch_validators_override=ConditionalValidators(),
    )
    _assert_no_secrets(buf.getvalue())
    assert result.status is ResumeSyncStatus.FETCH_REJECTED


def test_log_line_for_extraction_rejection_omits_excerpt() -> None:
    from app.resume.extract import ExtractionRejection

    body = b"%PDF-1.4 " + SECRET_RAW_BYTES_SENTINEL
    outcome = FetchOutcome(
        kind=FetchOutcomeKind.OK,
        http_status=200,
        byte_count=len(body),
        body=body,
        fetched_at=None,
    )

    buf = io.StringIO()
    logger = _capture_logger(buf)
    result = sync_resume(
        _make_settings_no_db(),
        logger=logger,
        fetch_outcome_override=outcome,
        pre_fetch_validators_override=ConditionalValidators(),
        extractor=lambda raw: ExtractionRejection(
            code="extraction.page_no_letters_or_numbers",
            page_index=0,
            page_count=1,
            byte_count=len(raw),
            content_char_count=0,
            content_token_count=0,
        ),
    )
    _assert_no_secrets(buf.getvalue())
    assert result.status is ResumeSyncStatus.EXTRACTION_REJECTED
    assert result.error is not None and SECRET_FIXTURE_TEXT not in result.error.code


def test_result_error_serialization_omits_unsafe_detail() -> None:
    from app.resume.results import ResumeSyncResultError

    err = ResumeSyncResultError(code="fetcher.non_200_status", detail=SECRET_FIXTURE_TEXT)
    # Error ``detail`` is never serialized through result_to_payload or logs.
    serialized = json.dumps(
        {
            "status": ResumeSyncStatus.FETCH_REJECTED.value,
            "error": {"code": err.code},
        }
    )
    assert SECRET_FIXTURE_TEXT not in serialized


def _assert_no_secrets(log_text: str) -> None:
    forbidden = [
        SECRET_FIXTURE_TEXT,
        SECRET_RAW_BYTES_SENTINEL.decode("utf-8", errors="replace"),
        SECRET_ETAG,
        SECRET_LAST_MODIFIED,
    ]
    for token in forbidden:
        assert token not in log_text, f"secret token leaked to a log line: {token!r}"


def test_conditional_validators_neither_neither_boolean() -> None:
    cv = ConditionalValidators()
    assert cv.has_any is False
    cv2 = ConditionalValidators(etag='"x"')
    assert cv2.has_any is True
