"""SQLAlchemy Core repository operations for the one-endpoint runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

metadata = MetaData()

source_endpoint = Table(
    "source_endpoint",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("company_id", BigInteger),
    Column("status", Text),
    Column("kind", Text),
    Column("token", Text),
    Column("region", Text),
    Column("base_url", Text),
    extend_existing=True,
)

pipeline_run = Table(
    "pipeline_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_kind", Text),
    Column("status", Text),
    Column("config_version", Text),
    Column("last_completed_step", Text),
    Column("counts", JSONB),
    Column("error", JSONB),
    Column("started_at", TIMESTAMP(timezone=True)),
    Column("finished_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)

source_fetch = Table(
    "source_fetch",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("run_id", BigInteger),
    Column("endpoint_id", BigInteger),
    Column("status", Text),
    Column("http_status", Integer),
    Column("postings_seen", Integer),
    Column("postings_new", Integer),
    Column("postings_changed", Integer),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("error", JSONB),
    Column("started_at", TIMESTAMP(timezone=True)),
    Column("finished_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)

ENDPOINT_COLUMNS = (
    source_endpoint.c.id,
    source_endpoint.c.company_id,
    source_endpoint.c.status,
    source_endpoint.c.kind,
    source_endpoint.c.token,
    source_endpoint.c.region,
    source_endpoint.c.base_url,
)


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    id: int
    company_id: object
    status: object
    kind: object
    token: object
    region: object
    base_url: object

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EndpointSnapshot:
        return cls(
            id=int(row["id"]),
            company_id=row["company_id"],
            status=row["status"],
            kind=row["kind"],
            token=row["token"],
            region=row["region"],
            base_url=row["base_url"],
        )


def reserved_counts(endpoint_id: int, adapter_kind: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "endpoint_id": endpoint_id,
        "adapter_kind": adapter_kind,
        "attempt_recorded": False,
        "source_fetch_id": None,
        "http_status": None,
        "postings_seen": 0,
        "postings_new": 0,
        "postings_changed": 0,
    }


def attempt_counts(result: Any) -> dict[str, object]:
    return {
        "schema_version": 1,
        "endpoint_id": result.source_endpoint_id,
        "adapter_kind": None,
        "attempt_recorded": True,
        "source_fetch_id": result.source_fetch_id,
        "http_status": result.http_status,
        "postings_seen": result.postings_seen,
        "postings_new": result.source_fetch_postings_new,
        "postings_changed": result.source_fetch_postings_changed,
    }


def lock_endpoint(conn: Connection, endpoint_id: int) -> EndpointSnapshot | None:
    row = (
        conn.execute(
            select(*ENDPOINT_COLUMNS)
            .where(source_endpoint.c.id == endpoint_id)
            .with_for_update(read=True)
        )
        .mappings()
        .first()
    )
    return EndpointSnapshot.from_row(dict(row)) if row is not None else None


def lock_run(conn: Connection, run_id: int) -> dict[str, Any] | None:
    row = (
        conn.execute(select(pipeline_run).where(pipeline_run.c.id == run_id).with_for_update())
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def create_run(
    conn: Connection,
    *,
    endpoint_id: int,
    adapter_kind: str,
    config_version: str,
    started_at: datetime,
) -> dict[str, Any]:
    row = (
        conn.execute(
            insert(pipeline_run)
            .values(
                run_kind="manual",
                status="running",
                config_version=config_version,
                last_completed_step=None,
                counts=reserved_counts(endpoint_id, adapter_kind),
                error=None,
                started_at=started_at,
                finished_at=None,
            )
            .returning(*pipeline_run.columns)
        )
        .mappings()
        .one()
    )
    return dict(row)


def validator_rows(
    conn: Connection,
    *,
    endpoint_id: int,
    excluded_run_id: int,
) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        select(
            source_fetch.c.id,
            source_fetch.c.status,
            source_fetch.c.http_status,
            source_fetch.c.error,
            source_fetch.c.etag,
            source_fetch.c.last_modified,
            source_fetch.c.finished_at,
        )
        .where(
            source_fetch.c.endpoint_id == endpoint_id,
            source_fetch.c.run_id != excluded_run_id,
            source_fetch.c.finished_at.is_not(None),
        )
        .order_by(source_fetch.c.finished_at.desc(), source_fetch.c.id.desc())
    ).mappings()
    return tuple(dict(row) for row in rows)


def matching_fetch_row(conn: Connection, *, run_id: int, endpoint_id: int) -> dict[str, Any] | None:
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


def finish_run(
    conn: Connection,
    *,
    run_id: int,
    status: str,
    step: str,
    counts: dict[str, object],
    error: dict[str, str] | None,
    finished_at: datetime,
) -> None:
    conn.execute(
        update(pipeline_run)
        .where(pipeline_run.c.id == run_id)
        .values(
            status=status,
            last_completed_step=step,
            counts=counts,
            error=error,
            finished_at=finished_at,
        )
    )
