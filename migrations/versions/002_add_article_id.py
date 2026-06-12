"""add article_id column to adjudications

Revision ID: 002_add_article_id
Revises: 001_create_adjudications
Create Date: 2026-06-12 00:00:00

Adds the nullable ``article_id`` column (VARCHAR(50)) to the
``adjudications`` table together with a B-tree index
``ix_adjudications_article_id``. The column carries the upstream
``id_articulo`` attribute from the BCompras XML report so it can be used
for exact-match filtering. Records whose XML element lacks the
attribute are stored with ``NULL``; the column therefore MUST be
nullable.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_add_article_id"
down_revision: str | Sequence[str] | None = "001_create_adjudications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "adjudications",
        sa.Column("article_id", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "ix_adjudications_article_id",
        "adjudications",
        ["article_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_adjudications_article_id", table_name="adjudications")
    op.drop_column("adjudications", "article_id")
