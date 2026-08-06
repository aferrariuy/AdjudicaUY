"""Add indexes used by dashboard aggregate queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "dashboard_aggregate_indexes"
down_revision: str | Sequence[str] | None = "oferente_company_document_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create portable indexes for dashboard aggregate filters."""

    op.create_index("ix_compra_organismo", "compra", ["organismo"])
    op.create_index(
        "ix_adjudicacion_nombre_comercial",
        "adjudicacion",
        ["nombre_comercial"],
    )


def downgrade() -> None:
    """Remove dashboard aggregate indexes in reverse creation order."""

    op.drop_index(
        "ix_adjudicacion_nombre_comercial",
        table_name="adjudicacion",
    )
    op.drop_index("ix_compra_organismo", table_name="compra")
