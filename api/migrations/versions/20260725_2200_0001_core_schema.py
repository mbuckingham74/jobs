"""Phase 1 core schema.

First domain revision for jobs.forkstech.com. Builds the PostgreSQL 16 +
pgvector tables described in specification section 04 that are required by the
Phase 1 queue path: company, source_endpoint, pipeline_run, source_fetch,
posting, posting_version, resume_version, candidate_profile_version,
company_research, score, daily_digest, and digest_item.

This migration is structural only. It defines the catalog and the constraints
that enforce the specified value domains, uniqueness rules, foreign-key delete
actions, and query indexes. It does not introduce SQLAlchemy ORM models,
application behavior, ingestion, scoring, or any out-of-scope feature.

The ``vector`` extension is installed before any vector column or index is
created and removed after the vector column and HNSW index are dropped in the
downgrade. ``discovery_event``, ``application``, and ``application_event``
belong to later phases and are intentionally absent.

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-07-25 22:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stable check-constraint names so later migrations and schema tests can refer
# to them. Naming every application-defined check, unique, and FK object keeps
# the catalog deterministic and keeps schema diffs reviewable.
_SOURCE_ENDPOINT_KIND_CHECK = (
    "kind in ('greenhouse','lever','ashby','workday','workable',"
    "'smartrecruiters','recruitee','custom')"
)
_SOURCE_ENDPOINT_STATUS_CHECK = "status in ('active','paused','failing','retired')"
_PIPELINE_RUN_KIND_CHECK = "run_kind in ('daily','manual','discovery','backfill')"
_PIPELINE_RUN_STATUS_CHECK = "status in ('running','succeeded','partial','failed')"
_SOURCE_FETCH_STATUS_CHECK = "status in ('complete','incomplete','failed')"
_RESUME_VARIANT_CHECK = "variant in ('base','product','program','technical')"
_RESUME_SOURCE_KIND_CHECK = "source_kind in ('portfolio_pdf','manual','n8n')"
_RESUME_PORTFOLIO_PDF_CHECK = (
    "source_kind <> 'portfolio_pdf' "
    "or (variant = 'base' and source_url is not null and source_sha256 is not null)"
)
_RESUME_N8N_PARENT_CHECK = "source_kind <> 'n8n' or parent_resume_version_id is not null"
_SCORE_STAGE_CHECK = "stage in ('filter','triage','deep')"
_SCORE_VERDICT_CHECK = "verdict in ('apply','maybe','skip')"
_DIGEST_ITEM_RANK_CHECK = "rank between 1 and 3"
_DIGEST_ITEM_STATE_CHECK = "state in ('recommended','skipped','applied','expired','restored')"

# A deep score must carry every versioned input the rubric needs. Filter and
# triage scores may omit these foreign keys; the check only fires for deep.
_DEEP_SCORE_VERSION_INPUTS = (
    "stage <> 'deep' or ("
    " resume_version_id is not null"
    " and candidate_profile_version_id is not null"
    " and company_research_id is not null"
    ")"
)

# A deep score must also carry an eligibility verdict. The JSON membership test
# evaluates to SQL NULL when the ``status`` key is missing or JSON null, and
# PostgreSQL treats a NULL check expression as a pass. Wrap the membership test
# with ``coalesce(..., false)`` so a missing status fails closed instead of
# silently passing through a NULL check result.
_DEEP_SCORE_LOCATION_STATUS = (
    "stage <> 'deep' or ("
    " location_eligibility is not null"
    " and coalesce("
    "   (location_eligibility ->> 'status') in ('eligible','ineligible','unclear'),"
    "   false"
    " )"
    ")"
)


def upgrade() -> None:
    # The pgvector extension must exist before the vector(1024) column and its
    # HNSW index are created.
    op.execute("create extension if not exists vector")

    op.create_table(
        "company",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("canonical_domain", sa.Text),
        sa.Column("careers_url", sa.Text),
        sa.Column("one_liner", sa.Text),
        sa.Column(
            "blocked",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("canonical_domain", name="company_canonical_domain_key"),
    )

    op.create_table(
        "source_endpoint",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "company_id",
            sa.BigInteger,
            sa.ForeignKey("company.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("token", sa.Text, nullable=False),
        sa.Column(
            "region",
            sa.Text,
            nullable=False,
            server_default=sa.text("'global'"),
        ),
        sa.Column("base_url", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "discovered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(_SOURCE_ENDPOINT_KIND_CHECK, name="source_endpoint_kind_check"),
        sa.CheckConstraint(_SOURCE_ENDPOINT_STATUS_CHECK, name="source_endpoint_status_check"),
        sa.UniqueConstraint(
            "kind", "token", "region", name="source_endpoint_kind_token_region_key"
        ),
    )

    op.create_table(
        "pipeline_run",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("run_kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("config_version", sa.Text, nullable=False),
        sa.Column("last_completed_step", sa.Text),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(_PIPELINE_RUN_KIND_CHECK, name="pipeline_run_run_kind_check"),
        sa.CheckConstraint(_PIPELINE_RUN_STATUS_CHECK, name="pipeline_run_status_check"),
    )

    op.create_table(
        "source_fetch",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger,
            sa.ForeignKey("pipeline_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            sa.BigInteger,
            sa.ForeignKey("source_endpoint.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("http_status", sa.Integer),
        sa.Column("postings_seen", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("postings_new", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "postings_changed",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("etag", sa.Text),
        sa.Column("last_modified", sa.Text),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(_SOURCE_FETCH_STATUS_CHECK, name="source_fetch_status_check"),
        sa.UniqueConstraint("run_id", "endpoint_id", name="source_fetch_run_id_endpoint_id_key"),
    )

    op.create_table(
        "posting",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "company_id",
            sa.BigInteger,
            sa.ForeignKey("company.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_endpoint_id",
            sa.BigInteger,
            sa.ForeignKey("source_endpoint.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("title_norm", sa.Text, nullable=False),
        sa.Column(
            "locations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_remote", sa.Boolean),
        sa.Column("remote_scope", sa.Text),
        sa.Column("remote_evidence", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("department", sa.Text),
        sa.Column("posting_url", sa.Text, nullable=False),
        sa.Column("apply_url", sa.Text, nullable=False),
        sa.Column("source_published_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("source_updated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "duplicate_of_id",
            sa.BigInteger,
            sa.ForeignKey("posting.id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint(
            "source_endpoint_id",
            "external_id",
            name="posting_source_endpoint_id_external_id_key",
        ),
    )

    op.create_table(
        "posting_version",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "posting_id",
            sa.BigInteger,
            sa.ForeignKey("posting.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "observed_in_run_id",
            sa.BigInteger,
            sa.ForeignKey("pipeline_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column(
            "locations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("description_md", sa.Text),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column("embedding_model", sa.Text),
        sa.Column(
            "observed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "posting_id",
            "content_hash",
            name="posting_version_posting_id_content_hash_key",
        ),
    )

    op.create_table(
        "resume_version",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("variant", sa.Text, nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("source_sha256", sa.Text),
        sa.Column("source_etag", sa.Text),
        sa.Column("source_last_modified", sa.TIMESTAMP(timezone=True)),
        sa.Column("source_fetched_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "parent_resume_version_id",
            sa.BigInteger,
            sa.ForeignKey("resume_version.id", ondelete="RESTRICT"),
        ),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("content_md", sa.Text, nullable=False),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(_RESUME_VARIANT_CHECK, name="resume_version_variant_check"),
        sa.CheckConstraint(_RESUME_SOURCE_KIND_CHECK, name="resume_version_source_kind_check"),
        sa.CheckConstraint(
            _RESUME_PORTFOLIO_PDF_CHECK,
            name="resume_version_portfolio_pdf_check",
        ),
        sa.CheckConstraint(_RESUME_N8N_PARENT_CHECK, name="resume_version_n8n_parent_check"),
        sa.UniqueConstraint("content_hash", name="resume_version_content_hash_key"),
    )

    op.create_table(
        "candidate_profile_version",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "content_hash",
            name="candidate_profile_version_content_hash_key",
        ),
    )

    op.create_table(
        "company_research",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "company_id",
            sa.BigInteger,
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.BigInteger,
            sa.ForeignKey("pipeline_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("model_id", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("web_search_tool_version", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "source_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "researched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "company_id",
            "content_hash",
            name="company_research_company_id_content_hash_key",
        ),
    )

    op.create_table(
        "score",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger,
            sa.ForeignKey("pipeline_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "posting_version_id",
            sa.BigInteger,
            sa.ForeignKey("posting_version.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resume_version_id",
            sa.BigInteger,
            sa.ForeignKey("resume_version.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "candidate_profile_version_id",
            sa.BigInteger,
            sa.ForeignKey("candidate_profile_version.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "company_research_id",
            sa.BigInteger,
            sa.ForeignKey("company_research.id", ondelete="RESTRICT"),
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("model_id", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("ruleset_version", sa.Text),
        sa.Column("rubric_version", sa.Text),
        sa.Column("input_hash", sa.Text, nullable=False),
        sa.Column("overall", sa.Numeric(5, 2)),
        sa.Column("domain_fit", sa.Numeric(4, 2)),
        sa.Column("seniority_fit", sa.Numeric(4, 2)),
        sa.Column("remote_fit", sa.Numeric(4, 2)),
        sa.Column("company_fit", sa.Numeric(4, 2)),
        sa.Column("axis_notes", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("verdict", sa.Text),
        sa.Column("location_eligibility", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("rationale", sa.Text),
        sa.Column("gaps", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("hooks", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("red_flags", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("resume_variant", sa.Text),
        sa.Column("reject_reasons", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "scored_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(_SCORE_STAGE_CHECK, name="score_stage_check"),
        sa.CheckConstraint(_SCORE_VERDICT_CHECK, name="score_verdict_check"),
        sa.CheckConstraint(
            _DEEP_SCORE_VERSION_INPUTS,
            name="deep_score_version_inputs",
        ),
        sa.CheckConstraint(
            _DEEP_SCORE_LOCATION_STATUS,
            name="deep_score_location_status",
        ),
        sa.UniqueConstraint(
            "posting_version_id",
            "stage",
            "input_hash",
            name="score_posting_version_id_stage_input_hash_key",
        ),
    )

    op.create_table(
        "daily_digest",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("digest_on", sa.Date, nullable=False),
        sa.Column(
            "run_id",
            sa.BigInteger,
            sa.ForeignKey("pipeline_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "frozen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("digest_on", name="daily_digest_digest_on_key"),
    )

    op.create_table(
        "digest_item",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "digest_id",
            sa.BigInteger,
            sa.ForeignKey("daily_digest.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "posting_id",
            sa.BigInteger,
            sa.ForeignKey("posting.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "score_id",
            sa.BigInteger,
            sa.ForeignKey("score.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default=sa.text("'recommended'"),
        ),
        sa.Column("carry_until", sa.Date),
        sa.Column("skip_reason", sa.Text),
        sa.CheckConstraint(_DIGEST_ITEM_RANK_CHECK, name="digest_item_rank_check"),
        sa.CheckConstraint(_DIGEST_ITEM_STATE_CHECK, name="digest_item_state_check"),
        sa.UniqueConstraint("digest_id", "rank", name="digest_item_digest_id_rank_key"),
        sa.UniqueConstraint(
            "digest_id",
            "posting_id",
            name="digest_item_digest_id_posting_id_key",
        ),
    )

    # ------------------------------------------------------------------
    # Query indexes (section 04).
    # ------------------------------------------------------------------
    op.create_index(
        "posting_last_seen_idx",
        "posting",
        [sa.text("last_seen_at DESC")],
    )
    op.create_index(
        "posting_open_idx",
        "posting",
        ["closed_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "posting_version_lookup_idx",
        "posting_version",
        ["posting_id", sa.text("observed_at DESC")],
    )
    op.create_index(
        "posting_version_embed_idx",
        "posting_version",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "score_lookup_idx",
        "score",
        ["posting_version_id", "stage", sa.text("scored_at DESC")],
    )

    # ------------------------------------------------------------------
    # Partial unique indexes: one active resume_version per variant, and one
    # active candidate_profile_version overall.
    # ------------------------------------------------------------------
    op.create_index(
        "resume_version_one_active_per_variant",
        "resume_version",
        ["variant"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "candidate_profile_one_active",
        "candidate_profile_version",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    # Drop application objects in reverse dependency order so no foreign key is
    # left pointing at a table that no longer exists. The vector extension is
    # removed last, after the vector column and HNSW index are gone.

    op.drop_index("candidate_profile_one_active", table_name="candidate_profile_version")
    op.drop_index("resume_version_one_active_per_variant", table_name="resume_version")
    op.drop_index("score_lookup_idx", table_name="score")
    op.drop_index("posting_version_embed_idx", table_name="posting_version")
    op.drop_index("posting_version_lookup_idx", table_name="posting_version")
    op.drop_index("posting_open_idx", table_name="posting")
    op.drop_index("posting_last_seen_idx", table_name="posting")

    op.drop_table("digest_item")
    op.drop_table("daily_digest")
    op.drop_table("score")
    op.drop_table("company_research")
    op.drop_table("candidate_profile_version")
    op.drop_table("resume_version")
    op.drop_table("posting_version")
    op.drop_table("posting")
    op.drop_table("source_fetch")
    op.drop_table("pipeline_run")
    op.drop_table("source_endpoint")
    op.drop_table("company")

    op.execute("drop extension if exists vector")
