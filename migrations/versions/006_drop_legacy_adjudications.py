"""drop the legacy adjudications table

Revision ID: 006_drop_legacy_adjudications
Revises: 005_expand_db_schema_xml
Create Date: 2026-06-15 07:00:00

The new ``compra`` / ``adjudicacion`` / ``oferente`` tables are now
populated by the scraper (the previous migration created them; this
one removes the old flat table once the consumers have all been
switched over). There is no data migration — the operator wipes the
DB and re-runs ``scrape_day_by_day.py`` to repopulate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "006_drop_legacy_adjudications"
down_revision: str | Sequence[str] | None = "005_expand_db_schema_xml"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_adjudications_date", table_name="adjudications")
    op.drop_index("ix_adjudications_article", table_name="adjudications")
    op.drop_index("ix_adjudications_article_id", table_name="adjudications")
    op.drop_index("ix_company", table_name="adjudications")
    op.drop_index("ix_organism", table_name="adjudications")
    op.drop_table("adjudications")


def downgrade() -> None:
    # Recreate the legacy table (DDL only). Downgrade is for emergency
    # rollback — re-population must run through the legacy pipeline.
    op.create_table(
        "adjudications",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_uyu", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("winning_company", sa.String(length=255), nullable=False),
        sa.Column("company_document", sa.String(length=50), nullable=True),
        sa.Column("company_document_type", sa.String(length=10), nullable=True),
        sa.Column("organism", sa.String(length=255), nullable=False),
        sa.Column("id_inciso", sa.Integer(), nullable=True),
        sa.Column("id_ue", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("license_type", sa.String(length=100), nullable=True),
        sa.Column("article", sa.String(length=255), nullable=False),
        sa.Column("article_quantity", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("article_id", sa.String(length=50), nullable=True),
        sa.Column("license_link", sa.String(length=512), nullable=True),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_adjudications_date", "adjudications", ["date"])
    op.create_index("ix_adjudications_article", "adjudications", ["article"])
    op.create_index(
        "ix_adjudications_article_id", "adjudications", ["article_id"]
    )
    op.create_index("ix_company", "adjudications", ["winning_company"])
    op.create_index("ix_organism", "adjudications", ["organism"])

