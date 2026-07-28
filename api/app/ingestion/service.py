"""Race-safe, first-write-wins ATS ingestion service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.ingestion.canonical import (
    InvalidObservation,
    PreparedFetch,
    prepare_fetch_result,
    retained_invalid_facts,
)
from app.ingestion.persistence import persist_posting
from app.ingestion.repository import (
    pipeline_run,
    posting,
    posting_version,
    source_endpoint,
    source_fetch,
)
from app.ingestion.results import (
    IngestionDatabaseError,
    IngestionInputError,
    IngestionResult,
    IngestionTargetError,
    TransportFailureCode,
)
from app.sources.ats.contracts import FetchResult

MutationHook = Callable[[str, Connection], None]

INVALID_OBSERVATION = "ingestion.invalid_observation"
STALE_OBSERVATION = "ingestion.stale_observation"
SUSPICIOUS_ZERO = "ingestion.suspicious_zero"
ZERO_VOLUME_ALERT = "ingestion.suspicious_zero"

logger = logging.getLogger("app.ingestion")


def _aware(value: datetime) -> bool:
    if value.tzinfo is None:
        return False
    try:
        return value.tzinfo.utcoffset(value) is not None
    except Exception:
        return False


def _validate_call_metadata(
    *,
    run_id: object,
    source_endpoint_id: object,
    started_at: object,
    finished_at: object,
) -> tuple[int, int, datetime, datetime]:
    if type(run_id) is not int or run_id <= 0:  # noqa: E721 - reject Boolean IDs
        raise IngestionInputError("ingestion.invalid_run_id")
    if type(source_endpoint_id) is not int or source_endpoint_id <= 0:  # noqa: E721
        raise IngestionInputError("ingestion.invalid_source_endpoint_id")
    if not isinstance(started_at, datetime) or not _aware(started_at):
        raise IngestionInputError("ingestion.invalid_started_at")
    if not isinstance(finished_at, datetime) or not _aware(finished_at):
        raise IngestionInputError("ingestion.invalid_finished_at")
    if finished_at < started_at:
        raise IngestionInputError("ingestion.invalid_timestamp_order")
    return run_id, source_endpoint_id, started_at, finished_at


def _reason(error: object) -> str | None:
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str):
            return code
    return None


def _replay_result(conn: Connection, row: dict[str, Any]) -> IngestionResult:
    version_ids = tuple(
        int(value)
        for value in conn.execute(
            select(posting_version.c.id)
            .join(posting, posting.c.id == posting_version.c.posting_id)
            .where(
                posting_version.c.observed_in_run_id == row["run_id"],
                posting.c.source_endpoint_id == row["endpoint_id"],
            )
            .order_by(posting_version.c.id)
        ).scalars()
    )
    return IngestionResult(
        source_fetch_id=int(row["id"]),
        run_id=int(row["run_id"]),
        source_endpoint_id=int(row["endpoint_id"]),
        source_fetch_status=str(row["status"]),
        http_status=row["http_status"],
        postings_seen=int(row["postings_seen"]),
        source_fetch_postings_new=int(row["postings_new"]),
        source_fetch_postings_changed=int(row["postings_changed"]),
        reason_code=_reason(row["error"]),
        postings_created=0,
        postings_updated=0,
        versions_created=0,
        postings_closed=0,
        postings_reopened=0,
        created_posting_version_ids=version_ids,
        reconciliation_ran=False,
        replayed=True,
        endpoint_marked_failing=False,
        endpoint_marked_active=False,
        alert_required=False,
        alert_code=None,
    )


def _fetch_by_key(
    conn: Connection,
    *,
    run_id: int,
    endpoint_id: int,
) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(source_fetch).where(
                source_fetch.c.run_id == run_id,
                source_fetch.c.endpoint_id == endpoint_id,
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


class IngestionService:
    """Ingest already-produced ATS observations using one supplied engine."""

    def __init__(
        self,
        engine: Engine,
        *,
        mutation_hook: MutationHook | None = None,
    ) -> None:
        self._engine = engine
        self._mutation_hook = mutation_hook

    def _hook(self, stage: str, conn: Connection) -> None:
        if self._mutation_hook is not None:
            self._mutation_hook(stage, conn)

    def lookup_replay_result(
        self,
        *,
        run_id: int,
        source_endpoint_id: int,
    ) -> IngestionResult | None:
        """Return Task 006's persisted replay projection without writing.

        This is the narrow workflow seam for checking the durable
        ``(run_id, source_endpoint_id)`` attempt key.  It intentionally reuses
        the existing lookup and replay reconstruction and never accepts or
        inspects a later ``FetchResult``.
        """

        if type(run_id) is not int or run_id <= 0:  # noqa: E721 - reject Boolean IDs
            raise IngestionInputError("ingestion.invalid_run_id")
        if type(source_endpoint_id) is not int or source_endpoint_id <= 0:  # noqa: E721
            raise IngestionInputError("ingestion.invalid_source_endpoint_id")
        endpoint_id = source_endpoint_id
        try:
            return self._initial_replay(run_id=run_id, endpoint_id=endpoint_id)
        except SQLAlchemyError:
            raise IngestionDatabaseError("ingestion.database_error") from None

    def _initial_replay(
        self,
        *,
        run_id: int,
        endpoint_id: int,
    ) -> IngestionResult | None:
        with self._engine.connect() as conn:
            row = _fetch_by_key(conn, run_id=run_id, endpoint_id=endpoint_id)
            return _replay_result(conn, row) if row is not None else None

    def _verify_targets(self, *, run_id: int, endpoint_id: int) -> None:
        with self._engine.connect() as conn:
            endpoint_exists = conn.execute(
                select(source_endpoint.c.id).where(source_endpoint.c.id == endpoint_id)
            ).first()
            if endpoint_exists is None:
                raise IngestionTargetError("ingestion.source_endpoint_not_found")
            run_exists = conn.execute(
                select(pipeline_run.c.id).where(pipeline_run.c.id == run_id)
            ).first()
            if run_exists is None:
                raise IngestionTargetError("ingestion.pipeline_run_not_found")

    def ingest_fetch_result(
        self,
        *,
        run_id: int,
        source_endpoint_id: int,
        fetch_result: FetchResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> IngestionResult:
        run_id, endpoint_id, started_at, finished_at = _validate_call_metadata(
            run_id=run_id,
            source_endpoint_id=source_endpoint_id,
            started_at=started_at,
            finished_at=finished_at,
        )
        try:
            replay = self._initial_replay(run_id=run_id, endpoint_id=endpoint_id)
            if replay is not None:
                return replay
            self._verify_targets(run_id=run_id, endpoint_id=endpoint_id)
            try:
                prepared = prepare_fetch_result(fetch_result)
                invalid_facts = None
            except (InvalidObservation, AttributeError, RecursionError, TypeError, ValueError):
                prepared = None
                invalid_facts = retained_invalid_facts(fetch_result)
            result = self._write_fetch(
                run_id=run_id,
                endpoint_id=endpoint_id,
                started_at=started_at,
                finished_at=finished_at,
                prepared=prepared,
                invalid_facts=invalid_facts,
                transport_code=None,
            )
        except IntegrityError:
            raise IngestionDatabaseError("ingestion.database_conflict") from None
        except SQLAlchemyError:
            raise IngestionDatabaseError("ingestion.database_error") from None
        self._log_result(result)
        return result

    def record_fetch_failure(
        self,
        *,
        run_id: int,
        source_endpoint_id: int,
        started_at: datetime,
        finished_at: datetime,
        reason_code: TransportFailureCode,
    ) -> IngestionResult:
        run_id, endpoint_id, started_at, finished_at = _validate_call_metadata(
            run_id=run_id,
            source_endpoint_id=source_endpoint_id,
            started_at=started_at,
            finished_at=finished_at,
        )
        try:
            replay = self._initial_replay(run_id=run_id, endpoint_id=endpoint_id)
            if replay is not None:
                return replay
            self._verify_targets(run_id=run_id, endpoint_id=endpoint_id)
            if not isinstance(reason_code, TransportFailureCode):
                raise IngestionInputError("ingestion.invalid_transport_failure_code")
            result = self._write_fetch(
                run_id=run_id,
                endpoint_id=endpoint_id,
                started_at=started_at,
                finished_at=finished_at,
                prepared=None,
                invalid_facts=None,
                transport_code=reason_code,
            )
        except IntegrityError:
            raise IngestionDatabaseError("ingestion.database_conflict") from None
        except SQLAlchemyError:
            raise IngestionDatabaseError("ingestion.database_error") from None
        self._log_result(result)
        return result

    def _locked_targets(
        self,
        conn: Connection,
        *,
        run_id: int,
        endpoint_id: int,
    ) -> dict[str, Any]:
        endpoint_row = (
            conn.execute(
                select(source_endpoint).where(source_endpoint.c.id == endpoint_id).with_for_update()
            )
            .mappings()
            .first()
        )
        if endpoint_row is None:
            raise IngestionTargetError("ingestion.source_endpoint_not_found")
        if (
            conn.execute(select(pipeline_run.c.id).where(pipeline_run.c.id == run_id)).first()
            is None
        ):
            raise IngestionTargetError("ingestion.pipeline_run_not_found")
        return dict(endpoint_row)

    def _write_fetch(
        self,
        *,
        run_id: int,
        endpoint_id: int,
        started_at: datetime,
        finished_at: datetime,
        prepared: PreparedFetch | None,
        invalid_facts: tuple[int | None, str | None, str | None] | None,
        transport_code: TransportFailureCode | None,
    ) -> IngestionResult:
        with self._engine.begin() as conn:
            endpoint_row = self._locked_targets(
                conn,
                run_id=run_id,
                endpoint_id=endpoint_id,
            )
            race_winner = _fetch_by_key(conn, run_id=run_id, endpoint_id=endpoint_id)
            if race_winner is not None:
                return _replay_result(conn, race_winner)

            if invalid_facts is not None:
                http_status, etag, last_modified = invalid_facts
                return self._insert_fetch_only(
                    conn,
                    run_id=run_id,
                    endpoint_id=endpoint_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="failed",
                    http_status=http_status,
                    etag=etag,
                    last_modified=last_modified,
                    reason_code=INVALID_OBSERVATION,
                )
            if transport_code is not None:
                return self._insert_fetch_only(
                    conn,
                    run_id=run_id,
                    endpoint_id=endpoint_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="failed",
                    http_status=None,
                    etag=None,
                    last_modified=None,
                    reason_code=transport_code.value,
                )
            assert prepared is not None
            return self._persist_prepared(
                conn,
                endpoint_row=endpoint_row,
                run_id=run_id,
                endpoint_id=endpoint_id,
                started_at=started_at,
                finished_at=finished_at,
                prepared=prepared,
            )

    def _insert_source_fetch(
        self,
        conn: Connection,
        *,
        run_id: int,
        endpoint_id: int,
        status: str,
        http_status: int | None,
        postings_seen: int,
        postings_new: int,
        postings_changed: int,
        etag: str | None,
        last_modified: str | None,
        reason_code: str | None,
        started_at: datetime,
        finished_at: datetime,
    ) -> int:
        fetch_id = conn.execute(
            insert(source_fetch)
            .values(
                run_id=run_id,
                endpoint_id=endpoint_id,
                status=status,
                http_status=http_status,
                postings_seen=postings_seen,
                postings_new=postings_new,
                postings_changed=postings_changed,
                etag=etag,
                last_modified=last_modified,
                error={"code": reason_code} if reason_code is not None else None,
                started_at=started_at,
                finished_at=finished_at,
            )
            .returning(source_fetch.c.id)
        ).scalar_one()
        self._hook("source_fetch", conn)
        return int(fetch_id)

    def _insert_fetch_only(
        self,
        conn: Connection,
        *,
        run_id: int,
        endpoint_id: int,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        http_status: int | None,
        etag: str | None,
        last_modified: str | None,
        reason_code: str,
    ) -> IngestionResult:
        fetch_id = self._insert_source_fetch(
            conn,
            run_id=run_id,
            endpoint_id=endpoint_id,
            status=status,
            http_status=http_status,
            postings_seen=0,
            postings_new=0,
            postings_changed=0,
            etag=etag,
            last_modified=last_modified,
            reason_code=reason_code,
            started_at=started_at,
            finished_at=finished_at,
        )
        return _first_result(
            source_fetch_id=fetch_id,
            run_id=run_id,
            endpoint_id=endpoint_id,
            status=status,
            http_status=http_status,
            postings_seen=0,
            postings_new=0,
            postings_changed=0,
            reason_code=reason_code,
        )

    @staticmethod
    def _ordering_eligible(
        conn: Connection,
        *,
        endpoint_id: int,
        started_at: datetime,
    ) -> bool:
        disqualifier = conn.execute(
            select(source_fetch.c.id)
            .where(
                source_fetch.c.endpoint_id == endpoint_id,
                or_(
                    source_fetch.c.finished_at.is_(None),
                    source_fetch.c.finished_at > started_at,
                ),
            )
            .limit(1)
        ).first()
        return disqualifier is None

    @staticmethod
    def _is_suspicious_zero(
        conn: Connection,
        *,
        endpoint_id: int,
        started_at: datetime,
    ) -> bool:
        average = conn.execute(
            select(func.avg(source_fetch.c.postings_seen)).where(
                source_fetch.c.endpoint_id == endpoint_id,
                source_fetch.c.status == "complete",
                source_fetch.c.http_status == 200,
                source_fetch.c.finished_at.is_not(None),
                source_fetch.c.finished_at <= started_at,
            )
        ).scalar_one()
        return average is not None and float(average) > 20

    @staticmethod
    def _previous_was_suspicious_zero(
        conn: Connection,
        *,
        endpoint_id: int,
    ) -> bool:
        previous = (
            conn.execute(
                select(
                    source_fetch.c.http_status,
                    source_fetch.c.postings_seen,
                    source_fetch.c.error,
                )
                .where(source_fetch.c.endpoint_id == endpoint_id)
                .order_by(source_fetch.c.finished_at.desc(), source_fetch.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return (
            previous is not None
            and previous["http_status"] == 200
            and previous["postings_seen"] == 0
            and _reason(previous["error"]) == SUSPICIOUS_ZERO
        )

    def _persist_prepared(
        self,
        conn: Connection,
        *,
        endpoint_row: dict[str, Any],
        run_id: int,
        endpoint_id: int,
        started_at: datetime,
        finished_at: datetime,
        prepared: PreparedFetch,
    ) -> IngestionResult:
        postings_seen = len(prepared.postings)
        if not self._ordering_eligible(
            conn,
            endpoint_id=endpoint_id,
            started_at=started_at,
        ):
            fetch_id = self._insert_source_fetch(
                conn,
                run_id=run_id,
                endpoint_id=endpoint_id,
                status="incomplete",
                http_status=prepared.http_status,
                postings_seen=postings_seen,
                postings_new=0,
                postings_changed=0,
                etag=prepared.etag,
                last_modified=prepared.last_modified,
                reason_code=STALE_OBSERVATION,
                started_at=started_at,
                finished_at=finished_at,
            )
            return _first_result(
                source_fetch_id=fetch_id,
                run_id=run_id,
                endpoint_id=endpoint_id,
                status="incomplete",
                http_status=prepared.http_status,
                postings_seen=postings_seen,
                postings_new=0,
                postings_changed=0,
                reason_code=STALE_OBSERVATION,
            )

        suspicious_zero = (
            prepared.complete
            and prepared.http_status == 200
            and postings_seen == 0
            and self._is_suspicious_zero(
                conn,
                endpoint_id=endpoint_id,
                started_at=started_at,
            )
        )
        previous_suspicious = (
            self._previous_was_suspicious_zero(conn, endpoint_id=endpoint_id)
            if suspicious_zero
            else False
        )

        created_postings = 0
        created_versions: list[int] = []
        changed_postings = 0
        reopened = 0
        updated_existing_ids: set[int] = set()

        if not suspicious_zero and prepared.http_status != 304:
            for observation in prepared.postings:
                mutation = persist_posting(
                    conn,
                    hook=self._hook,
                    company_id=int(endpoint_row["company_id"]),
                    endpoint_id=endpoint_id,
                    run_id=run_id,
                    finished_at=finished_at,
                    observation=observation,
                )
                created_postings += mutation["created"]
                changed_postings += mutation["changed"]
                reopened += mutation["reopened"]
                created_versions.extend(mutation["version_ids"])
                updated_existing_ids.update(mutation["updated_ids"])

        reconciliation_ran = (
            prepared.complete and prepared.http_status == 200 and not suspicious_zero
        )
        closed = 0
        if reconciliation_ran:
            close_conditions = [
                posting.c.source_endpoint_id == endpoint_id,
                posting.c.closed_at.is_(None),
                posting.c.last_seen_at < started_at,
            ]
            observed_ids = [item.external_id for item in prepared.postings]
            if observed_ids:
                close_conditions.append(posting.c.external_id.not_in(observed_ids))
            closed_ids = tuple(
                int(value)
                for value in conn.execute(
                    update(posting)
                    .where(and_(*close_conditions))
                    .values(closed_at=finished_at)
                    .returning(posting.c.id)
                ).scalars()
            )
            if closed_ids:
                self._hook("close", conn)
                updated_existing_ids.update(closed_ids)
                closed = len(closed_ids)

        endpoint_marked_failing = False
        endpoint_marked_active = False
        alert_required = False
        alert_code = None
        if suspicious_zero and previous_suspicious and endpoint_row["status"] != "failing":
            conn.execute(
                update(source_endpoint)
                .where(source_endpoint.c.id == endpoint_id)
                .values(status="failing")
            )
            self._hook("endpoint_status", conn)
            endpoint_marked_failing = True
            alert_required = True
            alert_code = ZERO_VOLUME_ALERT
        elif (
            prepared.complete
            and prepared.http_status == 200
            and postings_seen > 0
            and endpoint_row["status"] == "failing"
        ):
            conn.execute(
                update(source_endpoint)
                .where(source_endpoint.c.id == endpoint_id)
                .values(status="active")
            )
            self._hook("endpoint_status", conn)
            endpoint_marked_active = True

        status = (
            "complete"
            if prepared.complete and prepared.http_status == 200 and not suspicious_zero
            else "incomplete"
        )
        reason_code = SUSPICIOUS_ZERO if suspicious_zero else None
        fetch_id = self._insert_source_fetch(
            conn,
            run_id=run_id,
            endpoint_id=endpoint_id,
            status=status,
            http_status=prepared.http_status,
            postings_seen=postings_seen,
            postings_new=created_postings,
            postings_changed=changed_postings,
            etag=prepared.etag,
            last_modified=prepared.last_modified,
            reason_code=reason_code,
            started_at=started_at,
            finished_at=finished_at,
        )
        return IngestionResult(
            source_fetch_id=fetch_id,
            run_id=run_id,
            source_endpoint_id=endpoint_id,
            source_fetch_status=status,
            http_status=prepared.http_status,
            postings_seen=postings_seen,
            source_fetch_postings_new=created_postings,
            source_fetch_postings_changed=changed_postings,
            reason_code=reason_code,
            postings_created=created_postings,
            postings_updated=len(updated_existing_ids),
            versions_created=len(created_versions),
            postings_closed=closed,
            postings_reopened=reopened,
            created_posting_version_ids=tuple(sorted(created_versions)),
            reconciliation_ran=reconciliation_ran,
            replayed=False,
            endpoint_marked_failing=endpoint_marked_failing,
            endpoint_marked_active=endpoint_marked_active,
            alert_required=alert_required,
            alert_code=alert_code,
        )

    @staticmethod
    def _log_result(result: IngestionResult) -> None:
        logger.info(
            "ingestion.attempt.completed",
            extra={
                "event_name": "ingestion.attempt.completed",
                "source_fetch_id": result.source_fetch_id,
                "run_id": result.run_id,
                "source_endpoint_id": result.source_endpoint_id,
                "status": result.source_fetch_status,
                "http_status": result.http_status,
                "postings_seen": result.postings_seen,
                "postings_new": result.source_fetch_postings_new,
                "postings_changed": result.source_fetch_postings_changed,
                "postings_closed": result.postings_closed,
                "postings_reopened": result.postings_reopened,
                "replayed": result.replayed,
                "reason_code": result.reason_code,
                "alert_required": result.alert_required,
                "alert_code": result.alert_code,
            },
        )


def _first_result(
    *,
    source_fetch_id: int,
    run_id: int,
    endpoint_id: int,
    status: str,
    http_status: int | None,
    postings_seen: int,
    postings_new: int,
    postings_changed: int,
    reason_code: str | None,
) -> IngestionResult:
    return IngestionResult(
        source_fetch_id=source_fetch_id,
        run_id=run_id,
        source_endpoint_id=endpoint_id,
        source_fetch_status=status,
        http_status=http_status,
        postings_seen=postings_seen,
        source_fetch_postings_new=postings_new,
        source_fetch_postings_changed=postings_changed,
        reason_code=reason_code,
        postings_created=0,
        postings_updated=0,
        versions_created=0,
        postings_closed=0,
        postings_reopened=0,
        created_posting_version_ids=(),
        reconciliation_ran=False,
        replayed=False,
        endpoint_marked_failing=False,
        endpoint_marked_active=False,
        alert_required=False,
        alert_code=None,
    )


def ingest_fetch_result(
    *,
    engine: Engine,
    run_id: int,
    source_endpoint_id: int,
    fetch_result: FetchResult,
    started_at: datetime,
    finished_at: datetime,
) -> IngestionResult:
    """Ingest one already-produced fetch result without performing network I/O."""

    return IngestionService(engine).ingest_fetch_result(
        run_id=run_id,
        source_endpoint_id=source_endpoint_id,
        fetch_result=fetch_result,
        started_at=started_at,
        finished_at=finished_at,
    )


def record_fetch_failure(
    *,
    engine: Engine,
    run_id: int,
    source_endpoint_id: int,
    started_at: datetime,
    finished_at: datetime,
    reason_code: TransportFailureCode,
) -> IngestionResult:
    """Persist one privacy-safe transport failure without accepting an exception."""

    return IngestionService(engine).record_fetch_failure(
        run_id=run_id,
        source_endpoint_id=source_endpoint_id,
        started_at=started_at,
        finished_at=finished_at,
        reason_code=reason_code,
    )
