"""SQLAlchemy Core table mirrors used only by ATS ingestion."""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

source_endpoint = Table(
    "source_endpoint",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("company_id", BigInteger),
    Column("status", Text),
    extend_existing=True,
)

pipeline_run = Table(
    "pipeline_run",
    metadata,
    Column("id", BigInteger, primary_key=True),
    extend_existing=True,
)

source_fetch = Table(
    "source_fetch",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
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

posting = Table(
    "posting",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("company_id", BigInteger),
    Column("source_endpoint_id", BigInteger),
    Column("external_id", Text),
    Column("title", Text),
    Column("title_norm", Text),
    Column("locations", JSONB),
    Column("is_remote", Boolean),
    Column("remote_scope", Text),
    Column("remote_evidence", JSONB),
    Column("department", Text),
    Column("posting_url", Text),
    Column("apply_url", Text),
    Column("source_published_at", TIMESTAMP(timezone=True)),
    Column("source_updated_at", TIMESTAMP(timezone=True)),
    Column("first_seen_at", TIMESTAMP(timezone=True)),
    Column("last_seen_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("duplicate_of_id", BigInteger),
    Column("current_version_id", BigInteger),
    extend_existing=True,
)

posting_version = Table(
    "posting_version",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("posting_id", BigInteger),
    Column("observed_in_run_id", BigInteger),
    Column("content_hash", Text),
    Column("title", Text),
    Column("locations", JSONB),
    Column("description_md", Text),
    Column("raw_payload", JSONB),
    Column("embedding_model", Text),
    Column("observed_at", TIMESTAMP(timezone=True)),
    extend_existing=True,
)
