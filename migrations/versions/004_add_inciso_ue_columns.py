"""add id_inciso and id_ue columns to adjudications

Revision ID: 004_add_inciso_ue
Revises: 9d8a3aa781d0
Create Date: 2026-06-15 00:00:00

Adds nullable ``id_inciso`` (INTEGER) and ``id_ue`` (INTEGER) columns to
the ``adjudications`` table. These store the raw government identifiers
used to resolve the organism name via the static lookup table. They
enable re-mapping if the lookup table needs correction without
re-fetching XML, and provide an audit trail for traceability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "004_add_inciso_ue"
down_revision: str | Sequence[str] | None = "9d8a3aa781d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adjudications",
        sa.Column("id_inciso", sa.Integer(), nullable=True),
    )
    op.add_column(
        "adjudications",
        sa.Column("id_ue", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("adjudications", "id_ue")
    op.drop_column("adjudications", "id_inciso")
