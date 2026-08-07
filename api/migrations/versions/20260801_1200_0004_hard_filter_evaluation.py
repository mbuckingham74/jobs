"""Persist immutable deterministic hard-filter evaluations.

Revision ID: 0004_hard_filter_evaluation
Revises: 0003_posting_current_version
Create Date: 2026-08-01 12:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_hard_filter_evaluation"
down_revision: str | None = "0003_posting_current_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hard_filter_evaluation",
        sa.Column(
            "id",
            sa.BigInteger,
            sa.Identity(always=False),
        ),
        sa.Column("posting_id", sa.BigInteger, nullable=False),
        sa.Column("posting_version_id", sa.BigInteger, nullable=False),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("policy_manifest_hash", sa.Text, nullable=False),
        sa.Column("mutable_state_hash", sa.Text, nullable=False),
        sa.Column("result_hash", sa.Text, nullable=False),
        sa.Column("input_hash", sa.Text, nullable=False),
        sa.Column("policy_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluation_as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("eligible", sa.Boolean, nullable=False),
        sa.Column("rejection_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unknowns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("staleness_source", sa.Text, nullable=False),
        sa.Column("rule_outcomes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="hard_filter_evaluation_pkey"),
        sa.ForeignKeyConstraint(
            ["posting_id", "posting_version_id"],
            ["posting_version.posting_id", "posting_version.id"],
            name="hard_filter_evaluation_posting_version_owner_fkey",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("input_hash", name="hard_filter_evaluation_input_hash_key"),
        sa.CheckConstraint(
            "posting_id > 0 and posting_version_id > 0 and id > 0",
            name="hard_filter_evaluation_positive_ids_check",
        ),
        sa.CheckConstraint(
            "policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'",
            name="hard_filter_evaluation_policy_version_check",
        ),
        sa.CheckConstraint(
            "policy_manifest_hash ~ '^[0-9a-f]{64}$'",
            name="hard_filter_evaluation_policy_manifest_hash_check",
        ),
        sa.CheckConstraint(
            "mutable_state_hash ~ '^[0-9a-f]{64}$'",
            name="hard_filter_evaluation_mutable_state_hash_check",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="hard_filter_evaluation_input_hash_check"
        ),
        sa.CheckConstraint(
            "result_hash ~ '^[0-9a-f]{64}$'", name="hard_filter_evaluation_result_hash_check"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_manifest) = 'object'",
            name="hard_filter_evaluation_policy_manifest_object_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_payload) = 'object'",
            name="hard_filter_evaluation_input_payload_object_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(output_payload) = 'object'",
            name="hard_filter_evaluation_output_payload_object_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rejection_reasons) = 'array'",
            name="hard_filter_evaluation_rejection_reasons_array_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(unknowns) = 'array'", name="hard_filter_evaluation_unknowns_array_check"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rule_outcomes) = 'array'",
            name="hard_filter_evaluation_rule_outcomes_array_check",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(rule_outcomes) = 9",
            name="hard_filter_evaluation_rule_outcomes_count_check",
        ),
        sa.CheckConstraint(
            "staleness_source in ('source_published_at', 'first_seen_at')",
            name="hard_filter_evaluation_staleness_source_check",
        ),
        sa.CheckConstraint(
            "eligible = (jsonb_array_length(rejection_reasons) = 0)",
            name="hard_filter_evaluation_eligible_reasons_check",
        ),
    )
    op.create_index(
        "hard_filter_evaluation_current_lookup_idx",
        "hard_filter_evaluation",
        ["posting_id", "posting_version_id", "policy_version", sa.text("evaluation_as_of DESC")],
    )


def downgrade() -> None:
    op.drop_index("hard_filter_evaluation_current_lookup_idx", table_name="hard_filter_evaluation")
    op.drop_table("hard_filter_evaluation")
