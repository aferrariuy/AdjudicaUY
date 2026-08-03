"""Add a partial lookup index for provider document identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "company_document_index"
down_revision: str | Sequence[str] | None = "1573c3ee8233"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create a document-pair index, excluding incomplete identities."""

    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_adjudicacion_company_document",
            "adjudicacion",
            ["tipo_doc_prov", "nro_doc_prov"],
            postgresql_where=(
                "tipo_doc_prov IS NOT NULL AND nro_doc_prov IS NOT NULL"
            ),
        )
    else:
        # SQLite's test path receives a portable composite index. The
        # PostgreSQL-only partial predicate is intentionally omitted.
        op.create_index(
            "ix_adjudicacion_company_document",
            "adjudicacion",
            ["tipo_doc_prov", "nro_doc_prov"],
        )


def downgrade() -> None:
    """Remove the document-pair lookup index."""

    op.drop_index("ix_adjudicacion_company_document", table_name="adjudicacion")
