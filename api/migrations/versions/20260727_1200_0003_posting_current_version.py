"""Add the durable current posting-version pointer.

Revision ID: 0003_posting_current_version
Revises: 0002_resume_source_state
Create Date: 2026-07-27 12:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_posting_current_version"
down_revision: str | None = "0002_resume_source_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_VERSION_INDEX = "posting_current_version_id_idx"
VERSION_OWNERSHIP_KEY = "posting_version_posting_id_id_key"
CURRENT_VERSION_FOREIGN_KEY = "posting_current_version_owner_fkey"


def upgrade() -> None:
    op.add_column(
        "posting",
        sa.Column("current_version_id", sa.BigInteger, nullable=True),
    )
    op.create_index(
        CURRENT_VERSION_INDEX,
        "posting",
        ["current_version_id"],
    )
    op.create_unique_constraint(
        VERSION_OWNERSHIP_KEY,
        "posting_version",
        ["posting_id", "id"],
    )
    op.create_foreign_key(
        CURRENT_VERSION_FOREIGN_KEY,
        "posting",
        "posting_version",
        ["id", "current_version_id"],
        ["posting_id", "id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        UPDATE posting AS p
        SET current_version_id = (
            SELECT pv.id
            FROM posting_version AS pv
            WHERE pv.posting_id = p.id
            ORDER BY pv.observed_at DESC, pv.id DESC
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1
            FROM posting_version AS pv
            WHERE pv.posting_id = p.id
        )
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        CURRENT_VERSION_FOREIGN_KEY,
        "posting",
        type_="foreignkey",
    )
    op.drop_index(CURRENT_VERSION_INDEX, table_name="posting")
    op.drop_column("posting", "current_version_id")
    op.drop_constraint(
        VERSION_OWNERSHIP_KEY,
        "posting_version",
        type_="unique",
    )
