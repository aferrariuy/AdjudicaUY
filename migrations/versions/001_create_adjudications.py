"""create adjudications table

Revision ID: 001_create_adjudications
Revises:
Create Date: 2026-06-11 00:00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_create_adjudications"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adjudications",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_uyu", sa.Numeric(12, 2), nullable=True),
        sa.Column("winning_company", sa.String(length=255), nullable=False),
        sa.Column("organism", sa.String(length=255), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("license_type", sa.String(length=100), nullable=True),
        sa.Column("article", sa.String(length=255), nullable=False),
        sa.Column("article_quantity", sa.Numeric(10, 2), nullable=True),
        sa.Column("license_link", sa.String(length=512), nullable=True),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Inline indexes declared on the column (date, article) are emitted by
    # create_table above; the standalone composite / single-column indexes
    # below are added explicitly.
    op.create_index("ix_adjudications_date", "adjudications", ["date"])
    op.create_index("ix_adjudications_article", "adjudications", ["article"])
    op.create_index("ix_company", "adjudications", ["winning_company"])
    op.create_index("ix_organism", "adjudications", ["organism"])

    op.create_unique_constraint(
        "uq_source",
        "adjudications",
        ["source_url", "license_link", "date"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_source", "adjudications", type_="unique")
    op.drop_index("ix_organism", table_name="adjudications")
    op.drop_index("ix_company", table_name="adjudications")
    op.drop_index("ix_adjudications_article", table_name="adjudications")
    op.drop_index("ix_adjudications_date", table_name="adjudications")
    op.drop_table("adjudications")
