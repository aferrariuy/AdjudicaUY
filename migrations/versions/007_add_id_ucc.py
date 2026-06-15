"""add id_ucc column and index to compra

Revision ID: 007_add_id_ucc
Revises: 006_drop_legacy_adjudications
Create Date: 2026-06-15 03:30:00

Adds a nullable ``id_ucc`` (INTEGER) column to the ``compra`` table plus
a non-unique index ``ix_compra_id_ucc``. The column carries the
alternative organism identifier that some ``<compra>`` elements use
instead of the ``(id_inciso, id_ue)`` pair. The column is nullable
because most purchases still rely on the incumbent pair — no
backfill is required and the migration is safe to apply with existing
rows in place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "007_add_id_ucc"
down_revision: str | Sequence[str] | None = "006_drop_legacy_adjudications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "compra",
        sa.Column("id_ucc", sa.Integer(), nullable=True),
    )
    op.create_index("ix_compra_id_ucc", "compra", ["id_ucc"])


def downgrade() -> None:
    op.drop_index("ix_compra_id_ucc", table_name="compra")
    op.drop_column("compra", "id_ucc")
