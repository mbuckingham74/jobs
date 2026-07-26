"""Narrowly scoped résumé repository.

Reads and writes only the two tables this sync touches, using parameterized
SQLAlchemy Core statements (no ORM model layer, no general repositories for
unrelated tables, no application-startup schema creation). The
``resume_version`` and ``resume_source_state`` table definitions mirror the
Alembic catalog so the repository never interpolates configuration or résumé
data into SQL strings.

Every mutator runs inside the caller's transaction (which also holds the
transaction-scoped advisory lock), so a single ``commit()`` atomically applies
the new provenance row and the matching source-state observation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    MetaData,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.orm import Session

_metadata = MetaData()

resume_version_table = Table(
    "resume_version",
    _metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("variant", Text),
    Column("source_kind", Text),
    Column("source_url", Text),
    Column("source_sha256", Text),
    Column("source_etag", Text),
    Column("source_last_modified", TIMESTAMP(timezone=True)),
    Column("source_fetched_at", TIMESTAMP(timezone=True)),
    Column("content_hash", Text),
    Column("content_md", Text),
    Column("active", Boolean),
    Column("created_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)

resume_source_state_table = Table(
    "resume_source_state",
    _metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("variant", Text),
    Column("source_kind", Text),
    Column("source_url", Text),
    Column("source_etag", Text),
    Column("source_last_modified", TIMESTAMP(timezone=True)),
    Column("last_checked_at", TIMESTAMP(timezone=True)),
    Column("last_body_fetched_at", TIMESTAMP(timezone=True)),
    Column("last_body_sha256", Text),
    Column("current_resume_version_id", BigInteger),
    Column("created_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)


def find_source_state(
    session: Session,
    *,
    variant: str,
    source_kind: str,
    source_url: str,
) -> dict[str, Any] | None:
    """Return the matching ``resume_source_state`` row or ``None``.

    Selected (or, when absent, created by the caller) by the unique
    ``(variant, source_kind, source_url)`` key. Validators are never borrowed
    from another URL's row.
    """

    stmt = select(resume_source_state_table).where(
        resume_source_state_table.c.variant == variant,
        resume_source_state_table.c.source_kind == source_kind,
        resume_source_state_table.c.source_url == source_url,
    )
    row = session.execute(stmt).mappings().first()
    return dict(row) if row is not None else None


def create_source_state_row(
    session: Session,
    *,
    variant: str,
    source_kind: str,
    source_url: str,
    last_checked_at: datetime,
    created_at: datetime,
) -> dict[str, Any]:
    """Insert a fresh matching source-state row and return it.

    Only called under the advisory lock by the service. All validator/body
    columns start null (a brand-new source has observed nothing yet).
    """

    stmt = (
        insert(resume_source_state_table)
        .values(
            variant=variant,
            source_kind=source_kind,
            source_url=source_url,
            source_etag=None,
            source_last_modified=None,
            last_checked_at=last_checked_at,
            last_body_fetched_at=None,
            last_body_sha256=None,
            current_resume_version_id=None,
            created_at=created_at,
            updated_at=created_at,
        )
        .returning(*resume_source_state_table.columns)
    )
    row = session.execute(stmt).mappings().first()
    session.flush()
    return dict(row)


def update_source_state_on_304(
    session: Session,
    *,
    state_id: int,
    etag: str | None,
    last_modified: datetime | None,
    validator_returned: bool,
    last_checked_at: datetime,
    updated_at: datetime,
) -> None:
    """Apply a ``304 Not Modified`` source-state update.

    Update ``last_checked_at`` and ``updated_at`` always; replace only
    validators the response actually returned. An absent validator preserves
    the existing value (``unset`` below), and ``last_body_sha256``,
    ``last_body_fetched_at``, and ``current_resume_version_id`` are never
    touched on a 304.
    """

    values: dict[str, Any] = {
        "last_checked_at": last_checked_at,
        "updated_at": updated_at,
    }
    if validator_returned:
        # Replace only the validators actually returned. The other (absent)
        # validator is preserved by simply not being included in the update.
        values["source_etag"] = etag
        values["source_last_modified"] = last_modified
    session.execute(
        update(resume_source_state_table)
        .where(resume_source_state_table.c.id == state_id)
        .values(**values)
    )


def update_source_state_on_200(
    session: Session,
    *,
    state_id: int,
    etag: str | None,
    last_modified: datetime | None,
    last_body_sha256: str,
    last_body_fetched_at: datetime,
    last_checked_at: datetime,
    current_resume_version_id: int,
    updated_at: datetime,
) -> None:
    """Apply a ``200 OK`` source-state update.

    Validators are replaced (clearing either when the response omits it),
    ``last_body_sha256``, ``last_body_fetched_at``, ``last_checked_at``,
    ``updated_at``, and ``current_resume_version_id`` are set together.
    """

    session.execute(
        update(resume_source_state_table)
        .where(resume_source_state_table.c.id == state_id)
        .values(
            source_etag=etag,
            source_last_modified=last_modified,
            last_body_sha256=last_body_sha256,
            last_body_fetched_at=last_body_fetched_at,
            last_checked_at=last_checked_at,
            updated_at=updated_at,
            current_resume_version_id=current_resume_version_id,
        )
    )


def find_version_by_content_hash(
    session: Session,
    *,
    content_hash: str,
) -> dict[str, Any] | None:
    """Return the existing ``resume_version`` row with this content hash.

    A content-hash match is the idempotency key. ``current_resume_version_id``
    is later pointed at this row when the content is byte-only-identical or an
    exact repeat.
    """

    stmt = select(resume_version_table).where(resume_version_table.c.content_hash == content_hash)
    row = session.execute(stmt).mappings().first()
    return dict(row) if row is not None else None


def exists_active_base_version(session: Session) -> bool:
    stmt = (
        select(resume_version_table.c.id)
        .where(
            resume_version_table.c.variant == "base",
            resume_version_table.c.active.is_(True),
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def exists_any_base_version(session: Session) -> bool:
    stmt = (
        select(resume_version_table.c.id).where(resume_version_table.c.variant == "base").limit(1)
    )
    return session.execute(stmt).first() is not None


def insert_resume_version(
    session: Session,
    *,
    variant: str,
    source_kind: str,
    source_url: str,
    source_sha256: str,
    source_etag: str | None,
    source_last_modified: datetime | None,
    source_fetched_at: datetime,
    content_hash: str,
    content_md: str,
    active: bool,
    created_at: datetime,
) -> int:
    """Insert one immutable ``resume_version`` row and return its id.

    The row snapshots the exact URL, raw hash, validators, and body-fetch
    timestamp of the ``200`` response that created it. An absent response
    validator is captured as null. Called once per sync at most.
    """

    stmt = (
        insert(resume_version_table)
        .values(
            variant=variant,
            source_kind=source_kind,
            source_url=source_url,
            source_sha256=source_sha256,
            source_etag=source_etag,
            source_last_modified=source_last_modified,
            source_fetched_at=source_fetched_at,
            content_hash=content_hash,
            content_md=content_md,
            active=active,
            created_at=created_at,
        )
        .returning(resume_version_table.c.id)
    )
    row = session.execute(stmt).first()
    return int(row[0])
