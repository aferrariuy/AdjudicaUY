"""Add a composite index for adjudicacion purchase document lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "adjudicacion_compra_doc_index"
down_revision: str | Sequence[str] | None = "dashboard_aggregate_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the portable adjudicacion purchase/document index."""

    op.create_index(
        "ix_adjudicacion_compra_document",
        "adjudicacion",
        ["compra_id", "tipo_doc_prov", "nro_doc_prov"],
    )


def downgrade() -> None:
    """Remove the adjudicacion purchase/document index."""

    op.drop_index(
        "ix_adjudicacion_compra_document",
        table_name="adjudicacion",
    )
