"""Shared helpers for resume persistence and CLI tests.

Constructs deterministic ``ResumeSettings`` over the disposable
``TEST_DATABASE_URL``, mock :class:`FetchOutcome` values, mock extraction
results, and a small invented paragraph used by the persistence tests. These
helpers never touch the production portfolio URL or a private résumé.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime

import pytest

from app.resume.config import ResumeSettings
from app.resume.extract import ExtractionResult
from app.resume.fetch import FetchOutcome, FetchOutcomeKind
from app.resume.normalize import normalize_markdown

SETTINGS_URL = "https://example.invalid/portfolio.pdf"


def skip_without_test_db() -> str:
    import os

    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set; PostgreSQL tests run only against a "
            "disposable local database (see the task validation commands)."
        )
    return url


def settings_for(url: str = SETTINGS_URL) -> ResumeSettings:
    db_url = skip_without_test_db()
    return ResumeSettings(
        database_url=db_url,
        base_resume_url=url,
        resume_max_bytes=10 * 1024 * 1024,
    )


def make_extraction_result(content_md_text: str, page_count: int = 1) -> ExtractionResult:
    md = normalize_markdown(content_md_text)
    char_count = sum(1 for ch in md if unicodedata.category(ch)[0] in {"L", "N"})
    token_count = 0
    in_token = False
    for ch in md:
        if unicodedata.category(ch)[0] in {"L", "N"}:
            if not in_token:
                token_count += 1
                in_token = True
        else:
            in_token = False
    return ExtractionResult(
        page_count=page_count,
        page_normalized=(md,) * page_count,
        content_md=md,
        content_hash=hashlib.sha256(md.encode("utf-8")).hexdigest(),
        content_char_count=char_count,
        content_token_count=token_count,
    )


def ok_outcome(
    body: bytes,
    etag: str | None = '"v"',
    last_modified: datetime | None = None,
) -> FetchOutcome:
    return FetchOutcome(
        kind=FetchOutcomeKind.OK,
        http_status=200,
        etag=etag,
        etag_returned=etag is not None,
        last_modified=last_modified,
        last_modified_returned=last_modified is not None,
        byte_count=len(body),
        body=body,
        sent_validators=False,
        fetched_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
    )


def not_modified_outcome(
    etag: str | None = None, last_modified: datetime | None = None
) -> FetchOutcome:
    return FetchOutcome(
        kind=FetchOutcomeKind.NOT_MODIFIED,
        http_status=304,
        etag=etag,
        etag_returned=etag is not None,
        last_modified=last_modified,
        last_modified_returned=last_modified is not None,
        sent_validators=True,
        fetched_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
    )


CONTENT_A = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima. " + " ".join(
    f"wordfilledtokentext{i}" for i in range(80)
)
CONTENT_B = "second variant content with different invented token textual scope here. " + " ".join(
    f"anotherwordtokenrunset{i}" for i in range(80)
)


def assert_no_unsafe_fields(serialized: str | dict) -> None:
    """Assert a serialized CLI payload/log line carries no resume text, bytes,
    headers, validators, database URLs, or credentials."""

    forbidden = [
        "content_md",
        "password",
        "postgresql+psycopg",
        "ETag",
        "etag-will-be-present-in-error",
    ]
    text_repr = repr(serialized)
    for token in forbidden:
        assert token not in text_repr, f"unsafe token leaked: {token!r}"
