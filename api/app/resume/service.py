"""Portfolio résumé sync orchestration.

One callable, :func:`sync_resume`, that performs a single bounded conditional
fetch, local PDF extraction with deterministic normalization, and one short
transactional persistence step. The function returns a typed
:class:`ResumeSyncResult` and never terminates the process. Fetch, extraction,
and persistence are kept behind small interfaces so tests can replace the HTTP
boundary without live network access.

Concurrency safety: the write transaction acquires a transaction-scoped
PostgreSQL advisory lock under a documented stable integer key, re-reads the
matching source state and activation state under the lock, and commits before
returning success. The partial unique index
``resume_version_one_active_per_variant``, the unique
``resume_version.content_hash`` constraint, and the unique
``resume_source_state_variant_source_kind_source_url_key`` constraint remain
database backstops; an integrity race is rolled back and re-read into an
idempotent structured outcome or a provenance conflict.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.resume.config import (
    ADVISORY_LOCK_KEY_1,
    ADVISORY_LOCK_KEY_2,
    RESUME_SOURCE_KIND,
    RESUME_VARIANT,
    ResumeSettings,
)
from app.resume.db import DatabaseError, make_engine
from app.resume.extract import (
    ExtractionRejection,
    ExtractionResult,
    extract_and_normalize,
    sha256_hex_of,
)
from app.resume.fetch import (
    ConditionalValidators,
    FetchOutcome,
    fetch_resume,
)
from app.resume.logging import configure_logging, log_event, safe_hostname
from app.resume.repository import (
    create_source_state_row,
    exists_any_base_version,
    find_source_state,
    find_version_by_content_hash,
    insert_resume_version,
    update_source_state_on_200,
    update_source_state_on_304,
)
from app.resume.results import FetchOutcomeKind, ResumeSyncResult, ResumeSyncStatus

Fetcher = Callable[..., FetchOutcome]
Extractor = Callable[[bytes], ExtractionResult | ExtractionRejection]
NowFn = Callable[[], datetime]


class _ProvenanceConflict(Exception):
    """Internal signal: an existing content hash is owned by an incompatible
    variant/source_kind. The outer orchestrator converts this into a
    ``PROVENANCE_CONFLICT`` structured result and mutates neither table."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def sync_resume(
    settings: ResumeSettings,
    *,
    fetcher: Fetcher | None = None,
    extractor: Extractor | None = None,
    now_fn: NowFn | None = None,
    logger: logging.Logger | None = None,
    fetch_outcome_override: FetchOutcome | None = None,
) -> ResumeSyncResult:
    """Run one resume-sync attempt and return a structured result.

    Tests may inject ``fetcher``, ``extractor``, ``now_fn``, and a fixed
    ``fetch_outcome_override`` so HTTP and PDF boundaries can be replaced
    without live network access or synthetic PDF fixtures.
    """

    log = logger or configure_logging()
    fetch = fetcher or fetch_resume
    extract = extractor or extract_and_normalize
    now = now_fn or _now_utc

    host = safe_hostname(settings.base_resume_url)
    log_event(log, "resume.sync.start", source_host=host, variant=settings.variant)

    pre_fetch_validators = _read_pre_fetch_validators(settings, log)
    sent_validators = pre_fetch_validators.has_any

    if fetch_outcome_override is not None:
        fetch_outcome = fetch_outcome_override
    else:
        fetch_outcome = fetch(
            settings.base_resume_url,
            max_bytes=settings.resume_max_bytes,
            validators=pre_fetch_validators,
        )

    log_event(
        log,
        "resume.sync.fetch.completed",
        source_host=host,
        http_status=fetch_outcome.http_status,
        kind=fetch_outcome.kind.value,
        byte_count=fetch_outcome.byte_count,
        returned_validators=fetch_outcome.returned_validators,
        sent_validators=sent_validators,
    )

    if fetch_outcome.kind is FetchOutcomeKind.ERROR:
        return _error_result(
            ResumeSyncStatus.FETCH_ERROR,
            fetch_outcome,
            sent_validators,
            reason=fetch_outcome.reason,
        )

    if fetch_outcome.kind is FetchOutcomeKind.REJECTED:
        return ResumeSyncResult(
            status=ResumeSyncStatus.FETCH_REJECTED,
            http_status=fetch_outcome.http_status,
            byte_count=fetch_outcome.byte_count,
            sent_validators=sent_validators,
            error=_err(fetch_outcome.reason),
        )

    if fetch_outcome.kind is FetchOutcomeKind.NOT_MODIFIED:
        result = _persist_not_modified(settings, fetch_outcome, now, sent_validators)
        if result.status is ResumeSyncStatus.NOT_MODIFIED:
            log_event(
                log,
                "resume.sync.not_modified",
                status="not_modified",
                source_host=host,
                http_status=304,
                returned_validators=fetch_outcome.returned_validators,
                sent_validators=sent_validators,
            )
        else:
            db_reason = result.error.code if result.error else "db.error"
            log_event(log, "resume.sync.db.error", status="db_error", reason=db_reason)
        return result

    body = fetch_outcome.body or b""
    raw_sha256 = sha256_hex_of(body)
    extraction = extract(body)
    if isinstance(extraction, ExtractionRejection):
        log_event(
            log,
            "resume.sync.extraction.rejected",
            status="extraction_rejected",
            reason=extraction.code,
            source_host=host,
            http_status=200,
            byte_count=len(body),
            page_count=extraction.page_count,
        )
        return ResumeSyncResult(
            status=ResumeSyncStatus.EXTRACTION_REJECTED,
            http_status=200,
            byte_count=len(body),
            page_count=extraction.page_count,
            content_char_count=extraction.content_char_count,
            content_token_count=extraction.content_token_count,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
            error=_err(extraction.code),
        )

    try:
        result = _persist_ok(settings, fetch_outcome, extraction, raw_sha256, now, sent_validators)
    except _ProvenanceConflict:
        log_event(
            log,
            "resume.sync.provenance_conflict",
            status="provenance_conflict",
            source_host=host,
        )
        return ResumeSyncResult(
            status=ResumeSyncStatus.PROVENANCE_CONFLICT,
            http_status=200,
            byte_count=fetch_outcome.byte_count,
            page_count=extraction.page_count,
            content_char_count=extraction.content_char_count,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
            error=_err("persistence.provenance_conflict"),
        )
    except DatabaseError as exc:
        log_event(log, "resume.sync.db.error", status="db_error", reason=exc.reason)
        return ResumeSyncResult(
            status=ResumeSyncStatus.DB_ERROR,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
            error=_err(exc.reason),
        )

    log_event(
        log,
        "resume.sync.persisted",
        status=result.status.value,
        source_host=host,
        http_status=200,
        byte_count=fetch_outcome.byte_count,
        page_count=extraction.page_count if result.ok else None,
        activated=result.activated,
        content_changed=result.content_changed,
        raw_changed=result.raw_changed,
        resume_version_id=result.resume_version_id,
        returned_validators=fetch_outcome.returned_validators,
        sent_validators=sent_validators,
    )
    return result


def _read_pre_fetch_validators(
    settings: ResumeSettings, log: logging.Logger
) -> ConditionalValidators:
    """Read the matching source-state row's validators for the next request.

    Never raises: a pre-fetch read failure (unreachable database, invalid
    connection string, transient query error) is treated as "no validators"
    so sync proceeds as an unconditional request against an unobserved source.
    The write transaction surfaces the real database error if the database is
    genuinely unavailable.
    """

    try:
        engine = make_engine(settings.database_url)
    except DatabaseError as exc:
        log_event(log, "resume.sync.db.invalid_url", reason=exc.reason, status="db_error")
        return ConditionalValidators()
    try:
        with engine.connect() as conn:
            row = conn.execute(_select_state_stmt(settings)).mappings().first()
    except Exception:  # noqa: BLE001 - Pre-fetch read failure surfaces as no validators
        engine.dispose()
        return ConditionalValidators()
    engine.dispose()
    if row is None:
        return ConditionalValidators()
    return ConditionalValidators(
        etag=row.get("source_etag"),
        last_modified=row.get("source_last_modified"),
    )


def _select_state_stmt(settings: ResumeSettings):
    from sqlalchemy import select

    from app.resume.repository import resume_source_state_table

    return select(resume_source_state_table).where(
        resume_source_state_table.c.variant == settings.variant,
        resume_source_state_table.c.source_kind == settings.source_kind,
        resume_source_state_table.c.source_url == settings.base_resume_url,
    )


def _error_result(
    status: ResumeSyncStatus,
    fetch_outcome: FetchOutcome,
    sent_validators: bool,
    *,
    reason: str,
) -> ResumeSyncResult:
    return ResumeSyncResult(
        status=status,
        http_status=fetch_outcome.http_status,
        sent_validators=sent_validators,
        error=_err(reason),
    )


def _open_locked_session(settings: ResumeSettings) -> tuple[Any, Session]:
    """Build an engine, open a connection-driven Session, and acquire the
    transaction-scoped advisory lock. Returns ``(engine, session)``; the
    caller owns the transaction via ``session.begin()`` and must dispose the
    engine."""

    engine = make_engine(settings.database_url)
    session = Session(engine)
    return engine, session


def _persist_not_modified(
    settings: ResumeSettings,
    fetch_outcome: FetchOutcome,
    now: NowFn,
    sent_validators: bool,
) -> ResumeSyncResult:
    try:
        engine, session = _open_locked_session(settings)
        with session.begin():
            session.execute(
                text("select pg_advisory_xact_lock(:k1, :k2)"),
                {"k1": ADVISORY_LOCK_KEY_1, "k2": ADVISORY_LOCK_KEY_2},
            )
            state = _select_or_create_state(session, settings, now)
            update_source_state_on_304(
                session,
                state_id=state["id"],
                etag=fetch_outcome.etag,
                last_modified=fetch_outcome.last_modified,
                validator_returned=fetch_outcome.returned_validators,
                last_checked_at=now(),
                updated_at=now(),
            )
        session.close()
        engine.dispose()
        return ResumeSyncResult(
            status=ResumeSyncStatus.NOT_MODIFIED,
            http_status=304,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
        )
    except DatabaseError as exc:
        return ResumeSyncResult(
            status=ResumeSyncStatus.DB_ERROR,
            http_status=304,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
            error=_err(exc.reason),
        )


def _persist_ok(
    settings: ResumeSettings,
    fetch_outcome: FetchOutcome,
    extraction: ExtractionResult,
    raw_sha256: str,
    now: NowFn,
    sent_validators: bool,
) -> ResumeSyncResult:
    """Run the locked persistence. Surfaces :class:`_ProvenanceConflict` and
    :class:`IntegrityError` to the caller for retry/structured-outcome
    handling."""

    engine, session = _open_locked_session(settings)
    try:
        result = _locked_decide_and_persist(
            session, settings, fetch_outcome, extraction, raw_sha256, now, sent_validators
        )
        session.commit()
        return result
    except IntegrityError:
        # Rollback and re-run the locked read once. The advisory lock would
        # normally serialize this path; an integrity race from an out-of-band
        # insert is reconciled to an unchanged-content or staged-inactive
        # outcome.
        session.rollback()
        try:
            result = _locked_decide_and_persist(
                session, settings, fetch_outcome, extraction, raw_sha256, now, sent_validators
            )
            session.commit()
            return result
        except IntegrityError:
            session.rollback()
            raise DatabaseError("db.integrity_unresolved") from None
        except _ProvenanceConflict:
            session.rollback()
            raise
    except _ProvenanceConflict:
        session.rollback()
        raise
    except DatabaseError:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def _locked_decide_and_persist(
    session: Session,
    settings: ResumeSettings,
    fetch_outcome: FetchOutcome,
    extraction: ExtractionResult,
    raw_sha256: str,
    now: NowFn,
    sent_validators: bool,
) -> ResumeSyncResult:
    """Acquire the advisory lock, re-read state, decide, and persist."""

    session.execute(
        text("select pg_advisory_xact_lock(:k1, :k2)"),
        {"k1": ADVISORY_LOCK_KEY_1, "k2": ADVISORY_LOCK_KEY_2},
    )
    state = _select_or_create_state(session, settings, now)
    prev_body_sha = state.get("last_body_sha256")
    raw_changed = (prev_body_sha is None) or (prev_body_sha != raw_sha256)

    existing = find_version_by_content_hash(session, content_hash=extraction.content_hash)
    if existing is not None:
        if (
            existing.get("variant") != RESUME_VARIANT
            or existing.get("source_kind") != RESUME_SOURCE_KIND
        ):
            raise _ProvenanceConflict()
        matched_id = int(existing["id"])
        update_source_state_on_200(
            session,
            state_id=state["id"],
            etag=fetch_outcome.etag,
            last_modified=fetch_outcome.last_modified,
            last_body_sha256=raw_sha256,
            last_body_fetched_at=fetch_outcome.fetched_at or now(),
            last_checked_at=now(),
            current_resume_version_id=matched_id,
            updated_at=now(),
        )
        return ResumeSyncResult(
            status=ResumeSyncStatus.UNCHANGED_CONTENT,
            resume_version_id=matched_id,
            http_status=200,
            sent_validators=sent_validators,
            returned_validators=fetch_outcome.returned_validators,
            content_changed=False,
            raw_changed=raw_changed,
            activated=False,
        )

    activate = not exists_any_base_version(session)
    fetched_at = fetch_outcome.fetched_at or now()
    new_id = insert_resume_version(
        session,
        variant=settings.variant,
        source_kind=settings.source_kind,
        source_url=settings.base_resume_url,
        source_sha256=raw_sha256,
        source_etag=fetch_outcome.etag,
        source_last_modified=fetch_outcome.last_modified,
        source_fetched_at=fetched_at,
        content_hash=extraction.content_hash,
        content_md=extraction.content_md,
        active=activate,
        created_at=now(),
    )
    update_source_state_on_200(
        session,
        state_id=state["id"],
        etag=fetch_outcome.etag,
        last_modified=fetch_outcome.last_modified,
        last_body_sha256=raw_sha256,
        last_body_fetched_at=fetched_at,
        last_checked_at=now(),
        current_resume_version_id=new_id,
        updated_at=now(),
    )
    status = ResumeSyncStatus.ACTIVATED_INITIAL if activate else ResumeSyncStatus.STAGED_FOR_REVIEW
    return ResumeSyncResult(
        status=status,
        resume_version_id=new_id,
        http_status=200,
        byte_count=fetch_outcome.byte_count,
        page_count=extraction.page_count,
        content_char_count=extraction.content_char_count,
        content_token_count=extraction.content_token_count,
        content_changed=True,
        raw_changed=raw_changed,
        activated=activate,
        sent_validators=sent_validators,
        returned_validators=fetch_outcome.returned_validators,
    )


def _select_or_create_state(
    session: Session,
    settings: ResumeSettings,
    now: NowFn,
) -> dict[str, Any]:
    state = find_source_state(
        session,
        variant=settings.variant,
        source_kind=settings.source_kind,
        source_url=settings.base_resume_url,
    )
    if state is None:
        state = create_source_state_row(
            session,
            variant=settings.variant,
            source_kind=settings.source_kind,
            source_url=settings.base_resume_url,
            last_checked_at=now(),
            created_at=now(),
        )
    return state


def _err(reason: str):
    from app.resume.results import ResumeSyncResultError

    return ResumeSyncResultError(code=reason or "error.unknown")
