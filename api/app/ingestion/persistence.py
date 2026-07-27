"""Posting and posting-version SQLAlchemy Core persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from app.ingestion.canonical import CanonicalPosting
from app.ingestion.repository import posting, posting_version

MutationHook = Callable[[str, Connection], None]


def persist_posting(
    conn: Connection,
    *,
    hook: MutationHook,
    company_id: int,
    endpoint_id: int,
    run_id: int,
    finished_at: datetime,
    observation: CanonicalPosting,
) -> dict[str, Any]:
    """Persist one endpoint-local observation and return exact mutation facts."""

    existing_row = (
        conn.execute(
            select(posting).where(
                posting.c.source_endpoint_id == endpoint_id,
                posting.c.external_id == observation.external_id,
            )
        )
        .mappings()
        .first()
    )
    locations = [dict(item) for item in observation.locations]
    if existing_row is None:
        posting_id = int(
            conn.execute(
                insert(posting)
                .values(
                    company_id=company_id,
                    source_endpoint_id=endpoint_id,
                    external_id=observation.external_id,
                    title=observation.title,
                    title_norm=observation.title_norm,
                    locations=locations,
                    is_remote=None,
                    remote_scope=None,
                    remote_evidence=None,
                    department=observation.department,
                    posting_url=observation.posting_url,
                    apply_url=observation.apply_url,
                    source_published_at=observation.source_published_at,
                    source_updated_at=observation.source_updated_at,
                    first_seen_at=finished_at,
                    last_seen_at=finished_at,
                    closed_at=None,
                    duplicate_of_id=None,
                    current_version_id=None,
                )
                .returning(posting.c.id)
            ).scalar_one()
        )
        hook("posting", conn)
        version_id = _insert_version(
            conn,
            hook=hook,
            posting_id=posting_id,
            run_id=run_id,
            finished_at=finished_at,
            observation=observation,
        )
        conn.execute(
            update(posting).where(posting.c.id == posting_id).values(current_version_id=version_id)
        )
        hook("posting", conn)
        return {
            "created": 1,
            "changed": 0,
            "reopened": 0,
            "version_ids": [version_id],
            "updated_ids": set(),
        }

    existing = dict(existing_row)
    posting_id = int(existing["id"])
    version_row = (
        conn.execute(
            select(posting_version.c.id).where(
                posting_version.c.posting_id == posting_id,
                posting_version.c.content_hash == observation.content_hash,
            )
        )
        .mappings()
        .first()
    )
    created_version_ids: list[int] = []
    if version_row is None:
        selected_version_id = _insert_version(
            conn,
            hook=hook,
            posting_id=posting_id,
            run_id=run_id,
            finished_at=finished_at,
            observation=observation,
        )
        created_version_ids.append(selected_version_id)
    else:
        selected_version_id = int(version_row["id"])

    pointer_changed = existing["current_version_id"] != selected_version_id
    reopened = existing["closed_at"] is not None and finished_at >= existing["closed_at"]
    new_last_seen = max(existing["last_seen_at"], finished_at)
    values: dict[str, Any] = {}
    if new_last_seen != existing["last_seen_at"]:
        values["last_seen_at"] = new_last_seen
    if reopened:
        values["closed_at"] = None
    if pointer_changed:
        values.update(
            current_version_id=selected_version_id,
            title=observation.title,
            title_norm=observation.title_norm,
            locations=locations,
            department=observation.department,
            posting_url=observation.posting_url,
            apply_url=observation.apply_url,
            source_published_at=observation.source_published_at,
            source_updated_at=observation.source_updated_at,
        )
    if values:
        conn.execute(update(posting).where(posting.c.id == posting_id).values(**values))
        hook("posting", conn)
    if reopened:
        hook("reopen", conn)
    return {
        "created": 0,
        "changed": int(pointer_changed),
        "reopened": int(reopened),
        "version_ids": created_version_ids,
        "updated_ids": {posting_id} if values else set(),
    }


def _insert_version(
    conn: Connection,
    *,
    hook: MutationHook,
    posting_id: int,
    run_id: int,
    finished_at: datetime,
    observation: CanonicalPosting,
) -> int:
    version_id = conn.execute(
        insert(posting_version)
        .values(
            posting_id=posting_id,
            observed_in_run_id=run_id,
            content_hash=observation.content_hash,
            title=observation.title,
            locations=[dict(item) for item in observation.locations],
            description_md=observation.description_md,
            raw_payload=observation.raw_payload,
            embedding_model=None,
            observed_at=finished_at,
        )
        .returning(posting_version.c.id)
    ).scalar_one()
    hook("version", conn)
    return int(version_id)
