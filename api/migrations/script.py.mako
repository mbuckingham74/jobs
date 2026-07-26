"""${message}

Revision ID: ${up_revision}
% if down_revision:
Revises: ${down_revision | comma,n}
% else:
Revises:
% endif
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence
% if upgrades or downgrades:
import sqlalchemy as sa
from alembic import op
% endif
${imports if imports else ""}
# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision).replace(chr(39), chr(34))}
down_revision: str | None = ${repr(down_revision).replace(chr(39), chr(34))}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels).replace(chr(39), chr(34))}
depends_on: str | Sequence[str] | None = ${repr(depends_on).replace(chr(39), chr(34))}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
